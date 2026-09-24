"""Export BU Cloud profile state (cookies + localStorage) using a caller-supplied token.

Mirrors the standalone exporter: boot a cloud browser bound to the profile, read the whole
cookie jar over CDP before any navigation, then read each origin's localStorage from a blank
page served for it (see ``seed``), so no site script runs and the real account is untouched.
sessionStorage and IndexedDB are not persisted by BU Cloud, so they are out of reach by design. The token is used only for these calls and is never written to disk.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from typing import Any, Callable

import httpx

from openbrowse.profiles.cdp import CdpUseAdapter
from openbrowse.profiles.seed import read_local_storage, visit_origins

CLOUD_API_BASE = os.environ.get("BROWSER_USE_CLOUD_API_URL", "https://api.browser-use.com").rstrip("/")


_cloud_env_lock = asyncio.Lock()


@contextlib.asynccontextmanager
async def _bu_token_env(token: str):
    # @nonobvious(forced-by) browser-use's cloud client reads BROWSER_USE_API_KEY from os.getenv; set it in memory only for the boot/teardown, serialised so concurrent exports can't clobber it, and always restore it.
    async with _cloud_env_lock:
        prev = os.environ.get("BROWSER_USE_API_KEY")
        os.environ["BROWSER_USE_API_KEY"] = token
        try:
            yield
        finally:
            if prev is None:
                os.environ.pop("BROWSER_USE_API_KEY", None)
            else:
                os.environ["BROWSER_USE_API_KEY"] = prev


def _map_cookie(c: dict[str, Any]) -> dict[str, Any]:
    out = {
        "name": c["name"],
        "value": c["value"],
        "domain": c["domain"],
        "path": c["path"],
        "expires": c.get("expires", -1),
        "httpOnly": c.get("httpOnly", False),
        "secure": c.get("secure", False),
        "sameSite": c.get("sameSite", "Lax"),
    }
    if c.get("partitionKey"):
        out["partitionKey"] = c["partitionKey"]
    return out


def _candidate_origins(hosts: list[str]) -> list[str]:
    out: set[str] = set()
    for h in hosts:
        h = h.lstrip(".")
        out.add(h)
        parts = h.split(".")
        if len(parts) > 2:
            apex = ".".join(parts[-2:])
            out.add(apex)
            out.add("www." + apex)
        else:
            out.add("www." + h)
    return [f"https://{h}" for h in sorted(out)]


async def list_cloud_profiles(token: str) -> list[dict[str, Any]]:
    """List the BU Cloud profiles for the given token: [{id, name, cookieDomains}]."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            f"{CLOUD_API_BASE}/api/v2/profiles",
            headers={"X-Browser-Use-API-Key": token},
        )
    if resp.status_code in (401, 403):
        raise PermissionError("BU Cloud rejected the token (invalid or unauthorised).")
    resp.raise_for_status()
    data = resp.json()
    items = data.get("items") or data.get("profiles") if isinstance(data, dict) else data
    profiles: list[dict[str, Any]] = []
    for p in items or []:
        pid = p.get("id") or p.get("profileId")
        if not pid:
            continue
        profiles.append(
            {
                "id": str(pid),
                "name": p.get("name"),
                "cookieDomains": p.get("cookieDomains") or p.get("cookie_domains") or [],
            }
        )
    return profiles


async def export_cloud_profile(
    token: str,
    profile_id: str,
    *,
    on_log: Callable[[str], None] | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> dict[str, Any]:
    """Boot a cloud browser for the profile and return its storage_state (cookies + origins).

    on_progress(done, total) is called through the localStorage sweep, where total is the number
    of candidate origins to check — the long part of an export.
    """
    from browser_use import BrowserProfile, BrowserSession
    from browser_use.browser.cloud.views import CloudBrowserParams

    log = on_log or (lambda _m: None)
    async with _bu_token_env(token):
        profile = BrowserProfile(
            use_cloud=True, cloud_browser_params=CloudBrowserParams(profile_id=profile_id)
        )
        session = BrowserSession(browser_profile=profile)
        log("booting cloud browser")
        await session.start()
        try:
            raw_cookies = await session._cdp_get_cookies()
            cookies = [_map_cookie(c) for c in raw_cookies]
            log(f"read {len(cookies)} cookies")
            origins = await _extract_local_storage(session, raw_cookies, log, on_progress)
            return {"cookies": cookies, "origins": origins}
        finally:
            with contextlib.suppress(Exception):
                await session.kill()


async def _extract_local_storage(
    session: Any,
    raw_cookies: list[dict[str, Any]],
    log: Callable[[str], None],
    on_progress: Callable[[int, int], None] | None = None,
) -> list[dict[str, Any]]:
    hosts = sorted({c["domain"].lstrip(".") for c in raw_cookies})
    origins_to_check = _candidate_origins(hosts)
    if on_progress:
        on_progress(0, len(origins_to_check))
    visit = await visit_origins(
        CdpUseAdapter(session.cdp_client),
        origins_to_check,
        read_local_storage,
        on_progress=on_progress,
    )
    found = {origin: items for origin, items in visit.results.items() if items}
    key_total = sum(len(items) for items in found.values())
    log(f"captured localStorage on {len(found)} origins ({key_total} keys)")
    return [{"origin": origin, "localStorage": found[origin]} for origin in sorted(found)]
