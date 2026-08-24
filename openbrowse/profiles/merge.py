"""Three-way merge of storage states, so sessions sharing a profile keep each other's cookies.

Each session runs against its own copy of the profile's storage state and merges that copy back
when its browser closes. The merge is three-way — the baseline the session started from, the state
it ended with, and whatever the profile holds by the time it finishes — so a session writes back
only the keys it actually changed and leaves every other key as the profile now has it. Without
this, the last browser to close would flatten the profile with a jar that predates every login,
logout and cart change the other session made.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from openbrowse.profiles.storage import normalize_storage_state

_STORAGE_KINDS = ("localStorage", "sessionStorage")


def _cookie_key(cookie: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(cookie.get("name") or ""),
        str(cookie.get("domain") or cookie.get("url") or ""),
        str(cookie.get("path") or "/"),
    )


def _index_cookies(state: dict[str, Any] | None) -> dict[tuple[str, str, str], dict[str, Any]]:
    cookies = (state or {}).get("cookies") or []
    if not isinstance(cookies, list):
        return {}
    return {_cookie_key(c): c for c in cookies if isinstance(c, dict) and c.get("name")}


def _index_origins(state: dict[str, Any] | None) -> dict[str, dict[str, dict[str, Any]]]:
    """``{origin: {kind: {name: value}}}`` for every storage kind an origin carries."""
    indexed: dict[str, dict[str, dict[str, Any]]] = {}
    origins = (state or {}).get("origins") or []
    if not isinstance(origins, list):
        return indexed
    for entry in origins:
        if not isinstance(entry, dict):
            continue
        origin = entry.get("origin")
        if not origin:
            continue
        kinds = indexed.setdefault(str(origin), {})
        for kind in _STORAGE_KINDS:
            items = entry.get(kind)
            if not isinstance(items, list):
                continue
            pairs = kinds.setdefault(kind, {})
            for item in items:
                if isinstance(item, dict) and item.get("name") is not None:
                    pairs[str(item["name"])] = item.get("value")
    return indexed


def _merge_map(
    baseline: dict[Any, Any], ours: dict[Any, Any], theirs: dict[Any, Any]
) -> dict[Any, Any]:
    """Apply our changes since ``baseline`` on top of ``theirs``.

    A key we left untouched keeps whatever the profile now holds, so a session that
    only logged into one site cannot roll back another site's newer cookie.
    """
    result = dict(theirs)
    for key in set(ours) | set(baseline):
        in_ours = key in ours
        in_base = key in baseline
        if in_ours and (not in_base or ours[key] != baseline[key]):
            result[key] = ours[key]
        elif in_base and not in_ours:
            # @nonobvious(must-hold): we deleted it, but a session that finished
            # after our baseline may have written a newer value; theirs wins over
            # our delete, because a stale delete losing beats a fresh login losing.
            if key in theirs and theirs[key] != baseline[key]:
                continue
            result.pop(key, None)
    return result


def merge_storage_states(
    baseline: dict[str, Any] | None,
    ours: dict[str, Any] | None,
    theirs: dict[str, Any] | None,
) -> dict[str, Any]:
    """Merge one session's storage state back into the profile's current state."""
    merged_cookies = _merge_map(
        _index_cookies(baseline), _index_cookies(ours), _index_cookies(theirs)
    )
    cookies = [merged_cookies[key] for key in sorted(merged_cookies, key=lambda k: (k[1], k[2], k[0]))]

    base_origins = _index_origins(baseline)
    our_origins = _index_origins(ours)
    their_origins = _index_origins(theirs)

    origins: list[dict[str, Any]] = []
    for origin in sorted(set(base_origins) | set(our_origins) | set(their_origins)):
        entry: dict[str, Any] = {"origin": origin}
        for kind in _STORAGE_KINDS:
            merged = _merge_map(
                base_origins.get(origin, {}).get(kind, {}),
                our_origins.get(origin, {}).get(kind, {}),
                their_origins.get(origin, {}).get(kind, {}),
            )
            if merged:
                entry[kind] = [{"name": name, "value": merged[name]} for name in sorted(merged)]
        if len(entry) > 1:
            origins.append(entry)

    return normalize_storage_state({"cookies": cookies, "origins": origins})


def read_state(path: Path) -> dict[str, Any] | None:
    """Read a storage-state file; None when it is absent, unreadable or not an object."""
    try:
        state = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    return state if isinstance(state, dict) else None


def write_state(path: Path, state: dict[str, Any]) -> None:
    """Write a storage state atomically, so a reader never sees a half-written jar."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / (path.name + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
