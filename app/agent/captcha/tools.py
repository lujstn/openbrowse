"""The solve_captcha action and its registration.

Detection is authoritative; the model's arguments are optional hints only. This
mirrors the register_* closure idiom used by the other tool families.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any
from urllib.parse import urlparse

from browser_use import ActionResult, BrowserSession, Tools

from app.agent.browser_cdp import _emit_progress, _eval_js
from app.agent.captcha import cdp
from app.agent.captcha.base import Detection, SolveContext
from app.agent.captcha.pipeline import run_solve
from app.agent.captcha.registry import detect_captcha, strategy_for
from app.config import settings

logger = logging.getLogger(__name__)

_SOLVE_DESCRIPTION = (
    "Solve the CAPTCHA blocking the current page. Nothing else solves it for you "
    "here, so call this the moment any CAPTCHA or verification challenge stands "
    "between you and the page you need. You do not need to name the type: the page "
    "is inspected and the right solver chosen. If you can read a site key off the "
    "widget you may pass it as hint_site_key."
)


def _apply_overrides(
    det: Detection | None, hint_type: str | None, hint_site_key: str | None
) -> Detection | None:
    if det is None and hint_type and strategy_for(hint_type):
        det = Detection(kind=hint_type, params={}, confidence=1)
    if det is None:
        return None
    if hint_site_key and not det.params.get("siteKey"):
        det = replace(det, params={**det.params, "siteKey": hint_site_key})
    return det


async def _build_ctx(browser_session: BrowserSession, det: Detection, progress: Any, cost_sink):
    try:
        current_url = await _eval_js(browser_session, "window.location.href") or ""
    except Exception:
        current_url = ""
    parsed = urlparse(current_url)
    host = parsed.netloc
    if det.interstitial and parsed.scheme:
        page_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    else:
        page_url = current_url

    async def emit(msg: str) -> None:
        await _emit_progress(progress, msg)

    cookies = await cdp.page_cookie_header(browser_session, host)
    return SolveContext(
        session=browser_session,
        page_url=page_url,
        host=host,
        cookies=cookies,
        emit=emit,
        cost_sink=cost_sink,
        proxy=getattr(settings, "captcha_proxy", "") or "",
    )


def register_captcha_tools(
    tools: Tools, cost_sink: list[float] | None = None, progress: Any = None
) -> None:
    """Register the CAPTCHA-solving action on a Tools instance.

    Each solved challenge's real cost is appended to ``cost_sink``; ``progress`` is
    an async callable receiving each attempt's outcome for the session feed.
    """
    if not settings.capsolver_api_key:
        logger.warning("CAPSOLVER_API_KEY not set — CAPTCHA solving disabled")
        return

    giveups: dict[str, int] = {}

    @tools.action(_SOLVE_DESCRIPTION)
    async def solve_captcha(
        browser_session: BrowserSession,
        hint_type: str | None = None,
        hint_site_key: str | None = None,
    ) -> ActionResult:
        det = await detect_captcha(browser_session)
        det = _apply_overrides(det, hint_type, hint_site_key)
        if det is None:
            return ActionResult(
                extracted_content=(
                    "No CAPTCHA was detected on this page. You may have misjudged; "
                    "carry on with the task."
                )
            )
        strategy = strategy_for(det.kind)
        if strategy is None:
            return ActionResult(error=f"No solver registered for {det.kind}")
        ctx = await _build_ctx(browser_session, det, progress, cost_sink)
        return await run_solve(strategy, det, ctx, giveups)
