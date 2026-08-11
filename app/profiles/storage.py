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

from app.config import settings

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


def normalize_storage_state(raw: Any) -> dict[str, Any]:
    """Return a clean ``{"cookies": [...], "origins": [...]}`` storage state.

    Cookies are reduced to CDP CookieParam-valid fields and malformed entries dropped.
    ``origins`` (localStorage/sessionStorage) are preserved verbatim for browser-use to restore.
    """
    if not isinstance(raw, dict):
        raise ValueError("storage state must be a JSON object")
    cookies_in = raw.get("cookies") or []
    if not isinstance(cookies_in, list):
        raise ValueError("storage state 'cookies' must be a list")
    cookies_out = [c for c in (_normalise_cookie(c) for c in cookies_in) if c is not None]
    origins = raw.get("origins")
    if not isinstance(origins, list):
        origins = []
    return {"cookies": cookies_out, "origins": origins}


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
