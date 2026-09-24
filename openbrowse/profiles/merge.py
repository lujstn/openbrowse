"""Three-way merge of storage states, so sessions sharing a profile keep each other's changes.

A session remembers the profile as it found it (the baseline), and when it writes back it
applies only what it changed since then on top of whatever the profile holds by now. A key
it never touched keeps the profile's current value, so the last browser to close cannot
roll back a login, logout or cart change another session made in the meantime.
"""

from __future__ import annotations

from typing import Any, Callable, Iterable

from openbrowse.profiles.policy import cookie_key
from openbrowse.profiles.storage import normalize_storage_state


def _index_cookies(state: dict[str, Any] | None) -> dict[tuple[str, str, str, str], dict[str, Any]]:
    cookies = (state or {}).get("cookies") or []
    if not isinstance(cookies, list):
        return {}
    return {cookie_key(c): c for c in cookies if isinstance(c, dict) and c.get("name")}


def _index_origins(state: dict[str, Any] | None) -> dict[str, dict[str, str]]:
    indexed: dict[str, dict[str, str]] = {}
    for entry in (state or {}).get("origins") or []:
        if isinstance(entry, dict) and entry.get("origin"):
            indexed[str(entry["origin"])] = {
                str(item["name"]): item.get("value")
                for item in entry.get("localStorage") or []
                if isinstance(item, dict) and item.get("name") is not None
            }
    return indexed


def cookie_meaning(cookie: dict[str, Any]) -> tuple[Any, ...]:
    """What a cookie means to a site, ignoring the bookkeeping fields Chrome adds
    when it reads a jar back (priority, source scheme and port, fractional expiry)."""
    expires = cookie.get("expires")
    lasts = round(expires) if isinstance(expires, (int, float)) and expires > 0 else -1
    return (
        cookie.get("value"),
        bool(cookie.get("secure")),
        bool(cookie.get("httpOnly")),
        cookie.get("sameSite") or "",
        lasts,
    )


def _merge_map(
    baseline: dict[Any, Any],
    ours: dict[Any, Any],
    theirs: dict[Any, Any],
    meaning: Callable[[Any], Any] = lambda value: value,
) -> dict[Any, Any]:
    """Apply our changes since ``baseline`` on top of ``theirs``."""
    result = dict(theirs)
    for key in set(ours) | set(baseline):
        in_ours = key in ours
        in_base = key in baseline
        if in_ours and (not in_base or meaning(ours[key]) != meaning(baseline[key])):
            result[key] = ours[key]
        elif in_base and not in_ours:
            # @nonobvious(must-hold): we deleted it, but a session that finished
            # after our baseline may have written a newer value; theirs wins over
            # our delete, because a stale delete losing beats a fresh login losing.
            if key in theirs and meaning(theirs[key]) != meaning(baseline[key]):
                continue
            result.pop(key, None)
    return result


def merge_storage_states(
    baseline: dict[str, Any] | None,
    ours: dict[str, Any] | None,
    theirs: dict[str, Any] | None,
    *,
    origins: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Merge one session's storage state back into the profile's current state.

    ``origins`` names the origins whose storage ``ours`` actually read; every other
    origin keeps what the profile holds, since a session that never opened a site
    says nothing about that site's storage. None means ``ours`` speaks for every origin.
    """
    merged_cookies = _merge_map(
        _index_cookies(baseline), _index_cookies(ours), _index_cookies(theirs), cookie_meaning
    )
    base_origins = _index_origins(baseline)
    our_origins = _index_origins(ours)
    their_origins = _index_origins(theirs)
    spoken_for = set(our_origins) | set(base_origins) if origins is None else set(origins)

    merged_origins: list[dict[str, Any]] = []
    for origin in sorted(set(their_origins) | spoken_for):
        if origin in spoken_for:
            items = _merge_map(
                base_origins.get(origin, {}),
                our_origins.get(origin, {}),
                their_origins.get(origin, {}),
            )
        else:
            items = their_origins[origin]
        if items:
            merged_origins.append(
                {
                    "origin": origin,
                    "localStorage": [{"name": k, "value": items[k]} for k in sorted(items)],
                }
            )

    return normalize_storage_state(
        {"cookies": list(merged_cookies.values()), "origins": merged_origins}
    )
