"""The shape of a profile's browser state, and where it lives.

A profile holds a Playwright/browser-use ``storage_state`` document at
``data/profiles/{id}.json``: ``{"cookies": [...], "origins": [...]}``, where each origin
carries its ``localStorage`` items. Keeping that shape means a jar exported from BU Cloud,
Playwright or browser-use imports as is, and one read out of here loads anywhere else.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from openbrowse.config import settings

# @nonobvious(forced-by) CDP Network.CookieParam accepts these; response-only getCookies fields (size, session) are dropped so setCookies does not reject the jar.
_COOKIE_PARAM_FIELDS = {
    "name", "value", "url", "domain", "path", "secure", "httpOnly",
    "sameSite", "expires", "priority", "sameParty", "sourceScheme",
    "sourcePort", "partitionKey",
}

_SAME_SITE = {"strict": "Strict", "lax": "Lax", "none": "None", "no_restriction": "None"}
_DEFAULT_PORTS = {"http": 80, "https": 443}


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


def normalise_origin(raw: Any) -> str | None:
    """``scheme://host[:port]`` the way ``location.origin`` spells it, or None for
    anything that is not a web origin a page could have stored data under."""
    if not isinstance(raw, str):
        return None
    try:
        parts = urlsplit(raw.strip())
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if scheme not in _DEFAULT_PORTS or not host:
        return None
    if port is not None and port != _DEFAULT_PORTS[scheme]:
        return f"{scheme}://{host}:{port}"
    return f"{scheme}://{host}"


def origin_host(origin: str) -> str:
    return urlsplit(origin).hostname or ""


def _normalise_origins(raw: Any) -> list[dict[str, Any]]:
    """Each origin once, with only its localStorage.

    sessionStorage is dropped: a browser keeps it for the life of a tab, never
    across a restart, so carrying it into a later session is not browser behaviour.
    """
    if not isinstance(raw, list):
        return []
    merged: dict[str, dict[str, str]] = {}
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        origin = normalise_origin(entry.get("origin"))
        items = entry.get("localStorage")
        if origin is None or not isinstance(items, list):
            continue
        pairs = merged.setdefault(origin, {})
        for item in items:
            if isinstance(item, dict) and item.get("name") is not None:
                value = item.get("value")
                pairs[str(item["name"])] = "" if value is None else str(value)
    return [
        {"origin": origin, "localStorage": [{"name": k, "value": v} for k, v in sorted(pairs.items())]}
        for origin, pairs in sorted(merged.items())
        if pairs
    ]


def normalize_storage_state(raw: Any) -> dict[str, Any]:
    """Return a clean ``{"cookies": [...], "origins": [...]}`` storage state.

    Cookies are reduced to CDP CookieParam-valid fields and malformed entries dropped;
    origins keep their localStorage only.
    """
    if not isinstance(raw, dict):
        raise ValueError("storage state must be a JSON object")
    cookies_in = raw.get("cookies") or []
    if not isinstance(cookies_in, list):
        raise ValueError("storage state 'cookies' must be a list")
    cookies_out = [c for c in (_normalise_cookie(c) for c in cookies_in) if c is not None]
    return {"cookies": cookies_out, "origins": _normalise_origins(raw.get("origins"))}


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
