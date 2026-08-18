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
