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

from openbrowse.agent.browser_cdp import _emit_progress, _eval_js
from openbrowse.agent.captcha import cdp
from openbrowse.agent.captcha.base import Detection, SolveContext
from openbrowse.agent.captcha.pipeline import run_solve
from openbrowse.agent.captcha.registry import detect_captcha, strategy_for
from openbrowse.config import settings

logger = logging.getLogger(__name__)

_SOLVE_DESCRIPTION = (
    "Use the operator-configured, authorised, paid service to solve the CAPTCHA "
    "blocking the current page. Call this before touching any checkbox, slider, "
    "image grid, icon puzzle, or secondary verification challenge; wait for its "
    "result and never click tiles or drag a puzzle yourself. You do not need to name "
    "the type: the page is inspected and the right solver chosen. If you can read a "
    "site key you may pass hint_site_key. For a plain distorted-text image with no "
    "marker, pass hint_type='imagetotext' with hint_answer_selector, the CSS selector "
    "of its answer box, and hint_image_selector when it is not the first image."
)


def _apply_overrides(
    det: Detection | None,
    hint_type: str | None,
    hint_site_key: str | None,
    hint_answer_selector: str | None = None,
    hint_image_selector: str | None = None,
) -> Detection | None:
    if det is None and hint_type and strategy_for(hint_type):
        det = Detection(kind=hint_type, params={}, confidence=1)
    if det is None:
        return None
    extra: dict[str, Any] = {}
    if hint_site_key and not det.params.get("siteKey"):
        extra["siteKey"] = hint_site_key
    if hint_answer_selector:
        extra["answer_selector"] = hint_answer_selector
    if hint_image_selector:
        extra["image_selector"] = hint_image_selector
    if extra:
        det = replace(det, params={**det.params, **extra})
    return det


async def _build_ctx(browser_session: BrowserSession, det: Detection, progress: Any, cost_sink):
    # @nonobvious(forced-by): the solver mints a token for the address we name, so
    # it must be the document's own base address, which is what a rewriting proxy
    # rebases to the original page rather than to the host now serving it.
    try:
        addresses = await _eval_js(
            browser_session,
            "({base: document.baseURI || '', here: window.location.href || ''})",
        ) or {}
    except Exception:
        addresses = {}
    base = addresses.get("base") or ""
    here = addresses.get("here") or ""
    current_url = here or base
    if base:
        base_host = urlparse(base).netloc
        api_host = urlparse(det.params.get("apiOrigin") or "").netloc
        if base_host == urlparse(here).netloc:
            current_url = base
        elif api_host and base_host == api_host:
            # @nonobvious(must-hold): a document may name any address as its base and
            # the solve is billed against whatever we name, so a rebase is only
            # believed when it agrees with where the challenge itself runs.
            current_url = base
            logger.info(
                "solve_captcha: page is served by %s but rebases to %s, which matches "
                "the challenge origin",
                urlparse(here).netloc, base_host,
            )
        elif base_host:
            logger.warning(
                "solve_captcha: ignoring a rebase to %s that does not match the "
                "challenge origin %s",
                base_host, api_host or "unknown",
            )
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
    )


def register_captcha_tools(
    tools: Tools, cost_sink: list[float] | None = None, progress: Any = None
) -> None:
    """Register the CAPTCHA-solving action on a Tools instance.

    Each solved challenge's real cost is appended to ``cost_sink``; ``progress`` is
    an async callable receiving each attempt's outcome for the session feed.
    """
    if not settings.capsolver_api_key:
        # No banner at session start: most sessions never meet a CAPTCHA, so
        # the fact solving is off only matters at the moment one appears.
        @tools.action(_SOLVE_DESCRIPTION)
        async def solve_captcha() -> ActionResult:
            message = (
                "CAPTCHA solving is off: no CAPSOLVER_API_KEY is configured, so "
                "this challenge cannot be solved automatically."
            )
            if progress is not None:
                await progress(message)
            return ActionResult(error=message)

        return

    giveups: dict[str, int] = {}

    @tools.action(_SOLVE_DESCRIPTION)
    async def solve_captcha(
        browser_session,
        hint_type: str | None = None,
        hint_site_key: str | None = None,
        hint_answer_selector: str | None = None,
        hint_image_selector: str | None = None,
    ) -> ActionResult:
        det = await detect_captcha(browser_session)
        det = _apply_overrides(
            det, hint_type, hint_site_key, hint_answer_selector, hint_image_selector
        )
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
