"""Low-level CDP primitives shared by the tool modules and the captcha package.

Kept in their own module so the captcha package can reuse them without importing
the large tools module and risking a circular import.
"""

from __future__ import annotations

import logging
from typing import Any

from browser_use import BrowserSession

logger = logging.getLogger(__name__)


async def _eval_js(browser_session: BrowserSession, expression: str) -> Any:
    """Execute JavaScript via BrowserSession's CDP connection.

    @nonobvious(forced-by): browser-use 0.13.7's CDP client only supports the typed
    ``send.Runtime.evaluate(params=..., session_id=...)`` form via a per-target
    ``get_or_create_cdp_session()``, not ``send(method, params)``.
    """
    cdp_session = await browser_session.get_or_create_cdp_session()
    result = await cdp_session.cdp_client.send.Runtime.evaluate(
        params={"expression": expression, "returnByValue": True, "awaitPromise": True},
        session_id=cdp_session.session_id,
    )
    if result.get("exceptionDetails"):
        raise RuntimeError(f"JS error: {result['exceptionDetails']}")
    return result.get("result", {}).get("value")


async def _eval_on_target(
    browser_session: BrowserSession, target_id: str, expression: str
) -> Any:
    """Runtime.evaluate against a specific CDP target (a background tab or an OOPIF)."""
    sess = await browser_session.get_or_create_cdp_session(target_id, focus=False)
    result = await sess.cdp_client.send.Runtime.evaluate(
        params={"expression": expression, "returnByValue": True, "awaitPromise": True},
        session_id=sess.session_id,
    )
    if result.get("exceptionDetails"):
        raise RuntimeError(f"JS error: {result['exceptionDetails']}")
    return result.get("result", {}).get("value")


async def _same_process_frame(
    browser_session: BrowserSession, target_id: str, url_contains: str
) -> tuple[str, int] | None:
    """Locate a same-process iframe by URL substring and open an isolated world in
    it. Returns (frame_url, execution_context_id), or None when no such frame is
    in the tab's frame tree.

    @nonobvious(forced-by): Chromium only gives an iframe its own CDP target when
    it is cross-SITE (scheme plus registrable domain; ports and subdomains do not
    count). A cross-origin but same-site embed — careers.acme.com inside
    www.acme.com — stays in the parent's process, is invisible to
    Target.getTargets, and its content is still walled off from main-frame JS by
    the same-origin policy. An isolated world in the frame is the only read path.
    """
    needle = (url_contains or "").lower()
    if not needle:
        return None
    sess = await browser_session.get_or_create_cdp_session(target_id, focus=False)
    tree = await sess.cdp_client.send.Page.getFrameTree(session_id=sess.session_id)

    def _walk(node: dict) -> list[dict]:
        found = [node.get("frame") or {}]
        for child in node.get("childFrames") or []:
            found.extend(_walk(child))
        return found

    frames = _walk(tree.get("frameTree") or {})
    for frame in frames[1:]:  # frames[0] is the main frame, never a panel
        frame_url = frame.get("url") or ""
        if frame.get("id") and needle in frame_url.lower():
            try:
                world = await sess.cdp_client.send.Page.createIsolatedWorld(
                    params={"frameId": frame["id"]}, session_id=sess.session_id
                )
            except Exception:
                # An OOPIF's frameId is not creatable from the parent target;
                # those frames are read via their own target instead.
                logger.debug("createIsolatedWorld failed", exc_info=True)
                continue
            context_id = world.get("executionContextId")
            if context_id is not None:
                return frame_url, context_id
    return None


async def _eval_in_frame_world(
    browser_session: BrowserSession, target_id: str, context_id: int, expression: str
) -> Any:
    """Runtime.evaluate inside an isolated world created by _same_process_frame."""
    sess = await browser_session.get_or_create_cdp_session(target_id, focus=False)
    result = await sess.cdp_client.send.Runtime.evaluate(
        params={
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": True,
            "contextId": context_id,
        },
        session_id=sess.session_id,
    )
    if result.get("exceptionDetails"):
        raise RuntimeError(f"JS error: {result['exceptionDetails']}")
    return result.get("result", {}).get("value")


async def _iframe_targets(browser_session: BrowserSession) -> list[dict[str, str]]:
    cdp = await browser_session.get_or_create_cdp_session()
    targets = await cdp.cdp_client.send.Target.getTargets()
    return [
        {"targetId": t["targetId"], "url": t.get("url", "")}
        for t in targets.get("targetInfos", [])
        if t.get("type") == "iframe"
    ]


async def _emit_progress(progress: Any, message: str) -> None:
    if progress is None:
        return
    try:
        await progress(message)
    except Exception:
        logger.debug("_emit_progress failed", exc_info=True)
