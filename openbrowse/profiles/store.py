"""Reading and writing a profile, with its limits applied on every write.

Next to each ``{id}.json`` sits ``{id}.meta.json``, recording when a session last used
each site, or for a site no session has opened, when the profile first held its data.
Eviction and idle expiry go by these. It is separate so the profile itself
stays a plain storage_state that any Playwright or browser-use tool can load.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from openbrowse.profiles import policy
from openbrowse.profiles.storage import origin_host

logger = logging.getLogger(__name__)

_META_VERSION = 1
_locks: dict[str, asyncio.Lock] = {}


@dataclass
class ProfileData:
    state: dict[str, Any] = field(default_factory=lambda: {"cookies": [], "origins": []})
    last_used: dict[str, float] = field(default_factory=dict)
    added: dict[str, float] = field(default_factory=dict)


def meta_path(path: Path) -> Path:
    return path.with_name(path.stem + ".meta.json")


def lock(path: Path) -> asyncio.Lock:
    """The one lock every read-modify-write of this profile takes."""
    key = str(path.resolve())
    found = _locks.get(key)
    if found is None:
        found = _locks[key] = asyncio.Lock()
    return found


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def load(path: Path) -> ProfileData | None:
    """The profile at ``path``; None when there is no readable profile there."""
    state = _read_json(path)
    if not isinstance(state, dict):
        return None
    meta = _read_json(meta_path(path))
    raw = meta.get("origins") if isinstance(meta, dict) else None
    last_used: dict[str, float] = {}
    added: dict[str, float] = {}
    if isinstance(raw, dict):
        for origin, info in raw.items():
            if not isinstance(info, dict):
                continue
            for key, into in (("lastUsed", last_used), ("added", added)):
                if isinstance(info.get(key), (int, float)):
                    into[str(origin)] = float(info[key])
    return ProfileData(state=state, last_used=last_used, added=added)


def save(path: Path, data: ProfileData, *, now: float | None = None) -> policy.Applied:
    """Apply the profile's limits and write it, profile first and then its sidecar,
    each atomically, so a reader never sees half a write."""
    applied = policy.apply(
        data.state, data.last_used, now=time.time() if now is None else now, added=data.added
    )
    origins: dict[str, dict[str, float]] = {o: {"added": w} for o, w in applied.added.items()}
    origins.update({o: {"lastUsed": w} for o, w in applied.last_used.items()})
    _write_json(path, applied.state)
    _write_json(
        meta_path(path),
        {"version": _META_VERSION, "origins": dict(sorted(origins.items()))},
    )
    if applied.evicted_origins or applied.dropped_cookies:
        logger.info(
            "Profile %s: evicted stored data for %d site(s), dropped %d cookie(s)",
            path.stem,
            len(applied.evicted_origins),
            applied.dropped_cookies,
        )
    return applied


def remove(path: Path) -> None:
    for target in (path, meta_path(path)):
        target.unlink(missing_ok=True)


def move(old: Path, new: Path) -> None:
    if not old.exists():
        return
    new.parent.mkdir(parents=True, exist_ok=True)
    old.replace(new)
    if meta_path(old).exists():
        meta_path(old).replace(meta_path(new))


@dataclass
class Site:
    name: str
    cookies: int = 0
    storage_chars: int = 0
    last_used: float | None = None


def _site_of(host: str, sites: list[str]) -> str:
    for site in sites:
        if host == site or host.endswith("." + site):
            return site
    return host


def sites(data: ProfileData) -> list[Site]:
    """The profile's data grouped by site, the way a browser's site-data settings list it:
    a cookie domain and every origin under it count as one site."""
    hosts: set[str] = set()
    for cookie in data.state.get("cookies") or []:
        hosts.add(str(cookie.get("domain") or "").lstrip(".").lower().removeprefix("www."))
    for entry in data.state.get("origins") or []:
        hosts.add(origin_host(entry["origin"]).removeprefix("www."))
    hosts.discard("")
    roots: list[str] = []
    for host in sorted(hosts, key=lambda h: (h.count("."), h)):
        if _site_of(host, roots) == host:
            roots.append(host)
    grouped: dict[str, Site] = {}
    for cookie in data.state.get("cookies") or []:
        host = str(cookie.get("domain") or "").lstrip(".").lower().removeprefix("www.")
        if host:
            name = _site_of(host, roots)
            grouped.setdefault(name, Site(name)).cookies += 1
    for entry in data.state.get("origins") or []:
        name = _site_of(origin_host(entry["origin"]).removeprefix("www."), roots)
        site = grouped.setdefault(name, Site(name))
        site.storage_chars += policy.origin_chars(entry)
    for origin, when in data.last_used.items():
        name = _site_of(origin_host(origin).removeprefix("www."), roots)
        if name in grouped:
            site = grouped[name]
            site.last_used = max(site.last_used or 0.0, when)
    return sorted(grouped.values(), key=lambda s: s.name)


def without_site(data: ProfileData, site: str) -> ProfileData:
    """``data`` with every cookie and stored item belonging to ``site`` removed."""

    def belongs(host: str) -> bool:
        host = host.lower().removeprefix("www.")
        return host == site or host.endswith("." + site)

    return ProfileData(
        state={
            "cookies": [
                c
                for c in data.state.get("cookies") or []
                if not belongs(str(c.get("domain") or "").lstrip("."))
            ],
            "origins": [
                e for e in data.state.get("origins") or [] if not belongs(origin_host(e["origin"]))
            ],
        },
        last_used={o: w for o, w in data.last_used.items() if not belongs(origin_host(o))},
        added={o: w for o, w in data.added.items() if not belongs(origin_host(o))},
    )


def migrate(profiles_dir: Path) -> int:
    """Bring every profile written before the limits existed under them, once.

    The profile as it stood is kept beside it as ``{id}.json.pre-limits``. Returns how
    many profiles were migrated.
    """
    if not profiles_dir.is_dir():
        return 0
    migrated = 0
    for path in sorted(profiles_dir.glob("*.json")):
        if path.name.endswith(".meta.json") or meta_path(path).exists():
            continue
        data = load(path)
        if data is None:
            continue
        backup = path.with_name(path.name + ".pre-limits")
        if not backup.exists():
            backup.write_bytes(path.read_bytes())
        try:
            save(path, data)
        except ValueError:
            logger.warning("Profile %s is not a storage state; left as it is", path.stem)
            continue
        migrated += 1
    return migrated
