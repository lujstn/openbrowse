"""Export BU Cloud profile state (cookies + localStorage) using a caller-supplied token.

Mirrors the standalone exporter: boot a cloud browser bound to the profile, read the whole
cookie jar over CDP before any navigation, then recover each origin's localStorage by serving
a blank page for it (so the origin is established and readable with no site JS and no effect on
the real account). sessionStorage and IndexedDB are not persisted by BU Cloud, so they are out
of reach by design. The token is used only for these calls and is never written to disk.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
from typing import Any, Callable

import httpx

CLOUD_API_BASE = os.environ.get("BROWSER_USE_CLOUD_API_URL", "https://api.browser-use.com").rstrip("/")

_BLANK_BODY = base64.b64encode(b"<!doctype html><html><head></head><body></body></html>").decode()
_READ_LS = (
    "(()=>{const o={};for(let i=0;i<localStorage.length;i++){const k=localStorage.key(i);"
    "o[k]=localStorage.getItem(k);}return JSON.stringify(o);})()"
)

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
    cdp = await session.get_or_create_cdp_session()
    client = cdp.cdp_client
    sid = cdp.session_id

    def on_paused(event: dict[str, Any], session_id: str | None = None) -> None:
        rid = event.get("requestId") or event.get("request_id")
        if not rid:
            return

        async def _fulfill() -> None:
            # @nonobvious(forced-by) serving a blank page establishes the origin so its persisted localStorage is readable via CDP, with no site JS and no effect on the real logged-in account.
            try:
                await client.send.Fetch.fulfillRequest(
                    params={
                        "requestId": rid,
                        "responseCode": 200,
                        "responseHeaders": [{"name": "Content-Type", "value": "text/html; charset=utf-8"}],
                        "body": _BLANK_BODY,
                    },
                    session_id=session_id or sid,
                )
            except Exception:
                with contextlib.suppress(Exception):
                    await client.send.Fetch.continueRequest(
                        params={"requestId": rid}, session_id=session_id or sid
                    )

        asyncio.create_task(_fulfill())

    client.register.Fetch.requestPaused(on_paused)
    await client.send.Fetch.enable(params={"patterns": [{"urlPattern": "*"}]}, session_id=sid)
    await client.send.Runtime.enable(session_id=sid)
    await client.send.Page.enable(session_id=sid)

    hosts = sorted({c["domain"].lstrip(".") for c in raw_cookies})
    origins_to_check = _candidate_origins(hosts)
    total = len(origins_to_check)
    if on_progress:
        on_progress(0, total)
    found: dict[str, dict[str, str]] = {}
    for i, origin in enumerate(origins_to_check):
        try:
            await asyncio.wait_for(
                client.send.Page.navigate(params={"url": origin + "/"}, session_id=sid), timeout=15
            )
            await asyncio.sleep(0.5)
            cur = (
                await client.send.Runtime.evaluate(
                    params={"expression": "location.origin", "returnByValue": True}, session_id=sid
                )
            ).get("result", {}).get("value")
            lsj = (
                await client.send.Runtime.evaluate(
                    params={"expression": _READ_LS, "returnByValue": True}, session_id=sid
                )
            ).get("result", {}).get("value")
            ls = json.loads(lsj or "{}")
            if ls and cur:
                found.setdefault(cur, {}).update(ls)
        except Exception:
            pass
        finally:
            if on_progress:
                on_progress(i + 1, total)

    key_total = sum(len(d) for d in found.values())
    log(f"captured localStorage on {len(found)} origins ({key_total} keys)")
    return [
        {"origin": org, "localStorage": [{"name": k, "value": v} for k, v in d.items()]}
        for org, d in sorted(found.items())
        if d
    ]
