"""Profile storage-state helpers: normalise cookie jars, read/write profile cookie files.

A profile's cookies live in a Playwright/browser-use ``storage_state`` file at
``data/profiles/{id}.json`` — ``{"cookies": [...], "origins": [...]}``. browser-use applies
the cookies through CDP ``Storage.setCookies`` and restores each origin's localStorage and
sessionStorage, so the file is the single source of a profile's authenticated state.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from openbrowse.config import settings

# @nonobvious(forced-by) CDP Network.CookieParam accepts these; response-only getCookies fields (size, session) are dropped so setCookies does not reject the jar.
_COOKIE_PARAM_FIELDS = {
    "name", "value", "url", "domain", "path", "secure", "httpOnly",
    "sameSite", "expires", "priority", "sameParty", "sourceScheme",
    "sourcePort", "partitionKey",
}

_SAME_SITE = {"strict": "Strict", "lax": "Lax", "none": "None", "no_restriction": "None"}


def _normalise_cookie(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    if not raw.get("name") or raw.get("value") is None:
        return None
    if not raw.get("domain") and not raw.get("url"):
        return None
    cookie = {k: v for k, v in raw.items() if k in _COOKIE_PARAM_FIELDS}
    same_site = cookie.get("sameSite")
    if same_site is not None:
        mapped = _SAME_SITE.get(str(same_site).lower())
        if mapped is None:
            cookie.pop("sameSite", None)
        else:
            cookie["sameSite"] = mapped
    # @nonobvious(forced-by) Chrome rejects SameSite=None without Secure; drop the attribute, keep the cookie.
    if cookie.get("sameSite") == "None" and not cookie.get("secure"):
        cookie.pop("sameSite", None)
    return cookie


# @nonobvious(means): login state is small (session ids, JWTs of a few KB); a
# stored value this large is a site's cache, which a profile has no need to keep.
_MAX_STORAGE_VALUE_CHARS = 65_536
_MAX_ORIGIN_STORAGE_CHARS = 262_144
_STORAGE_KINDS = ("localStorage", "sessionStorage")


def _item_size(item: Any) -> int:
    if not isinstance(item, dict):
        return 0
    return len(str(item.get("name") or "")) + len(str(item.get("value") or ""))


def trim_origins(origins: Any) -> list[dict[str, Any]]:
    """Origins with every stored value over _MAX_STORAGE_VALUE_CHARS dropped, and
    each origin's storage cut to _MAX_ORIGIN_STORAGE_CHARS by dropping its largest
    values first; origins left with nothing are removed.

    browser-use restores each origin by injecting a script into every document the
    browser loads. A profile that had merged back 4.8 MB of sites' caches made that
    injection wedge page loads outright; nothing in a login needs values this size.
    """
    if not isinstance(origins, list):
        return []
    trimmed: list[dict[str, Any]] = []
    for entry in origins:
        if not isinstance(entry, dict) or not entry.get("origin"):
            continue
        out: dict[str, Any] = {k: v for k, v in entry.items() if k not in _STORAGE_KINDS}
        kept: list[tuple[str, dict[str, Any]]] = []
        for kind in _STORAGE_KINDS:
            items = entry.get(kind)
            if isinstance(items, list):
                kept.extend(
                    (kind, item)
                    for item in items
                    if isinstance(item, dict) and _item_size(item) <= _MAX_STORAGE_VALUE_CHARS
                )
        total = sum(_item_size(item) for _, item in kept)
        for kind, item in sorted(kept, key=lambda pair: _item_size(pair[1]), reverse=True):
            if total <= _MAX_ORIGIN_STORAGE_CHARS:
                break
            kept.remove((kind, item))
            total -= _item_size(item)
        for kind in _STORAGE_KINDS:
            items = [item for k, item in kept if k == kind]
            if items:
                out[kind] = items
        if any(kind in out for kind in _STORAGE_KINDS):
            trimmed.append(out)
    return trimmed


def normalize_storage_state(raw: Any) -> dict[str, Any]:
    """Return a clean ``{"cookies": [...], "origins": [...]}`` storage state.

    Cookies are reduced to CDP CookieParam-valid fields and malformed entries dropped.
    ``origins`` (localStorage/sessionStorage) are kept for browser-use to restore,
    trimmed by ``trim_origins``.
    """
    if not isinstance(raw, dict):
        raise ValueError("storage state must be a JSON object")
    cookies_in = raw.get("cookies") or []
    if not isinstance(cookies_in, list):
        raise ValueError("storage state 'cookies' must be a list")
    cookies_out = [c for c in (_normalise_cookie(c) for c in cookies_in) if c is not None]
    return {"cookies": cookies_out, "origins": trim_origins(raw.get("origins"))}


def cookie_domains(state: dict[str, Any] | None) -> list[str]:
    """Sorted distinct cookie domains (leading dots stripped) in a storage state."""
    if not state:
        return []
    domains = {
        (c.get("domain") or "").lstrip(".")
        for c in state.get("cookies", [])
        if isinstance(c, dict)
    }
    return sorted(d for d in domains if d)


def profile_state_path(profile_id: str) -> Path:
    return settings.profiles_dir / f"{profile_id}.json"


def read_state_file(storage_state_path: str | None) -> dict[str, Any] | None:
    """Read a profile's storage_state file (relative to data_dir); None if absent/unreadable."""
    if not storage_state_path:
        return None
    path = settings.data_dir / storage_state_path
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def write_profile_state(profile_id: str, state: dict[str, Any], *, backup: bool = True) -> Path:
    """Write a profile's storage_state atomically, backing up any existing file to .import-bak."""
    path = profile_state_path(profile_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    if backup and path.exists():
        (path.parent / (path.name + ".import-bak")).write_bytes(path.read_bytes())
    tmp = path.parent / (path.name + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return path
