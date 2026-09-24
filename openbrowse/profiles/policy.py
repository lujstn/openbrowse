"""How much of a browsing history a profile keeps, and what goes first.

The rules follow what a browser does with its own profile. Cookies live until they
expire, up to a jar-wide limit. A site's stored data is kept or evicted as a whole, least
recently used site first, because evicting some of a site's keys and not others can leave
the site in a state it never wrote. A site nobody has visited for a while loses its data,
as Safari does after a week without interaction.

The budgets are tighter than a desktop browser's because every session starts from an
empty Chrome and writes the profile back into it before the first page loads.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from openbrowse.profiles.storage import normalize_storage_state, origin_host

MAX_SITES = 50
MAX_STORAGE_CHARS = 2 * 1024 * 1024
MAX_SITE_CHARS = 1024 * 1024
SITE_IDLE_EXPIRY_S = 30 * 24 * 3600
MAX_COOKIES = 3000
# @nonobvious(forced-by): Chrome refuses a cookie whose name and value exceed 4096 bytes.
MAX_COOKIE_CHARS = 4096
_MAX_REMEMBERED_ORIGINS = 1000


@dataclass
class Applied:
    state: dict[str, Any]
    last_used: dict[str, float]
    added: dict[str, float] = field(default_factory=dict)
    evicted_origins: list[str] = field(default_factory=list)
    dropped_cookies: int = 0


def origin_chars(entry: dict[str, Any]) -> int:
    return sum(
        len(item["name"]) + len(item["value"]) for item in entry.get("localStorage") or []
    )


def storage_chars(state: dict[str, Any]) -> int:
    return sum(origin_chars(entry) for entry in state.get("origins") or [])


def _domain_matches(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def _host_last_used(last_used: dict[str, float]) -> dict[str, float]:
    hosts: dict[str, float] = {}
    for origin, when in last_used.items():
        host = origin_host(origin)
        if host:
            hosts[host] = max(hosts.get(host, 0.0), when)
    return hosts


def _cookie_last_used(cookie: dict[str, Any], hosts: dict[str, float]) -> float:
    domain = str(cookie.get("domain") or "").lstrip(".").lower()
    return max((when for host, when in hosts.items() if _domain_matches(host, domain)), default=0.0)


def _expired(cookie: dict[str, Any], now: float) -> bool:
    expires = cookie.get("expires")
    return isinstance(expires, (int, float)) and 0 < expires < now


def cookie_key(cookie: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(cookie.get("name") or ""),
        str(cookie.get("domain") or cookie.get("url") or ""),
        str(cookie.get("path") or "/"),
        repr(cookie.get("partitionKey")) if cookie.get("partitionKey") else "",
    )


def _apply_cookies(
    cookies: list[dict[str, Any]], hosts: dict[str, float], now: float
) -> tuple[list[dict[str, Any]], int]:
    unique: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for cookie in cookies:
        unique[cookie_key(cookie)] = cookie
    kept = [
        c
        for c in unique.values()
        if not _expired(c, now) and len(str(c["name"])) + len(str(c["value"])) <= MAX_COOKIE_CHARS
    ]
    if len(kept) > MAX_COOKIES:

        def freshness(cookie: dict[str, Any]) -> tuple[float, float]:
            expires = cookie.get("expires")
            lasts = expires if isinstance(expires, (int, float)) and expires > 0 else float("inf")
            return (_cookie_last_used(cookie, hosts), lasts)

        kept = sorted(kept, key=freshness, reverse=True)[:MAX_COOKIES]
    dropped = len(cookies) - len(kept)
    kept.sort(key=lambda c: (str(c.get("domain") or ""), str(c.get("path") or ""), str(c["name"])))
    return kept, dropped


def _fresh(stamps: dict[str, float] | None, now: float) -> dict[str, float]:
    return {
        origin: float(when)
        for origin, when in (stamps or {}).items()
        if isinstance(when, (int, float)) and now - when <= SITE_IDLE_EXPIRY_S
    }


def apply(
    state: Any,
    last_used: dict[str, float] | None,
    *,
    now: float,
    added: dict[str, float] | None = None,
) -> Applied:
    """The state a profile may keep.

    ``last_used`` records when a session last opened each origin; ``added`` when the
    profile first held data for an origin nobody has opened since. Only real use
    ever goes into ``last_used``, because an invented time would let a site nobody
    uses tie with one a session has just used, and win.
    """
    clean = normalize_storage_state(state)
    used = _fresh(last_used, now)
    first_held = _fresh(added, now)
    for entry in clean["origins"]:
        origin = entry["origin"]
        if origin not in (last_used or {}) and origin not in (added or {}):
            first_held[origin] = now
    first_held = {o: w for o, w in first_held.items() if o not in used}

    cookies, dropped_cookies = _apply_cookies(clean["cookies"], _host_last_used(used), now)
    cookie_domains = {str(c.get("domain") or "").lstrip(".").lower() for c in cookies}

    evicted: list[str] = []
    candidates: list[dict[str, Any]] = []
    for entry in clean["origins"]:
        origin = entry["origin"]
        if (origin not in used and origin not in first_held) or origin_chars(entry) > MAX_SITE_CHARS:
            evicted.append(origin)
        else:
            candidates.append(entry)

    def keep_first(entry: dict[str, Any]) -> tuple[bool, float, bool, int]:
        origin = entry["origin"]
        host = origin_host(origin)
        signed_in = any(_domain_matches(host, d) for d in cookie_domains if d)
        return (
            origin not in used,
            -used.get(origin, first_held.get(origin, 0.0)),
            not signed_in,
            origin_chars(entry),
        )

    kept: list[dict[str, Any]] = []
    total = 0
    for entry in sorted(candidates, key=keep_first):
        size = origin_chars(entry)
        if len(kept) < MAX_SITES and total + size <= MAX_STORAGE_CHARS:
            kept.append(entry)
            total += size
        else:
            evicted.append(entry["origin"])
    kept.sort(key=lambda e: e["origin"])

    if len(used) > _MAX_REMEMBERED_ORIGINS:
        newest = sorted(used, key=used.get, reverse=True)[:_MAX_REMEMBERED_ORIGINS]
        used = {origin: used[origin] for origin in newest}
    kept_origins = {e["origin"] for e in kept}
    return Applied(
        state={"cookies": cookies, "origins": kept},
        last_used=used,
        added={o: w for o, w in first_held.items() if o in kept_origins},
        evicted_origins=sorted(evicted),
        dropped_cookies=dropped_cookies,
    )
