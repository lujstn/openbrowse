"""The uniform solve pipeline: capture, build, create+poll, redeem, verify.

One path for every strategy. Cost is recorded on every CapSolver bill including
failures; retries are small and bounded; the give-up and cost-cap guards are
money guards, never a verdict on the site.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from typing import Any

import httpx

from browser_use import ActionResult

from app.agent.captcha import client
from app.agent.captcha.base import Detection, SolveContext, _first_present
from app.agent.captcha.registry import detect_captcha
from app.config import settings

logger = logging.getLogger(__name__)

_SINGLE_USE_KEYS = ("dataS", "challenge", "captchaId")
_POLL_ATTEMPTS = 60
_POLL_INTERVAL = 2.0


def _record_cost(result: dict[str, Any], ctx: SolveContext) -> None:
    if ctx.cost_sink is None:
        return
    cost = client.parse_cost(result)
    if cost:
        ctx.cost_sink.append(cost)


def _article(kind: str) -> str:
    return "an" if kind[:1].lower() in "aeiou" else "a"


def _fresh_single_use(fresh: Detection, old: Detection) -> bool:
    for k in _SINGLE_USE_KEYS:
        nv = fresh.params.get(k)
        if nv and nv != old.params.get(k):
            return True
    return False


def _stale_geetest_v3_error(result: ActionResult) -> bool:
    detail = (result.error or "").lower()
    return "challenge" in detail and (
        "old" in detail or "expired" in detail or "more than once" in detail
    )


async def _create_and_poll(
    client_http: httpx.AsyncClient, payload: dict[str, Any], strategy, det, ctx
) -> tuple[dict[str, Any] | None, float, ActionResult | None]:
    loop = asyncio.get_running_loop()
    started = loop.time()
    logger.info(
        "solve_captcha: creating %s (site_key=%s cookies=%s data_s=%s "
        "api_domain=%s proxy=%s url=%s)",
        payload.get("type"),
        bool(payload.get("websiteKey")),
        "cookies" in payload,
        "recaptchaDataSValue" in payload,
        payload.get("apiDomain") or False,
        bool(payload.get("proxy")),
        payload.get("websiteURL"),
    )
    result = await client.create_task(client_http, payload)
    if result.get("errorId", 0) != 0 and "cookies" in payload:
        lean = {k: v for k, v in payload.items() if k != "cookies"}
        result = await client.create_task(client_http, lean)
    if result.get("errorId", 0) != 0:
        detail = result.get("errorDescription", "unknown")
        await ctx.emit(f"captcha: CapSolver refused ({detail})")
        return None, 0.0, ActionResult(error=f"CapSolver error: {detail}")

    task_id = result.get("taskId")
    if not task_id:
        solution = result.get("solution") or {}
        _record_cost(result, ctx)
        if _first_present(solution, strategy.solution_keys) is None:
            return None, 0.0, ActionResult(error="CapSolver returned no usable solution")
        return solution, loop.time() - started, None

    for _ in range(_POLL_ATTEMPTS):
        await asyncio.sleep(_POLL_INTERVAL)
        result = await client.get_task_result(client_http, task_id)
        status = result.get("status")
        if status == "ready":
            _record_cost(result, ctx)
            solution = result.get("solution") or {}
            if _first_present(solution, strategy.solution_keys) is None:
                return None, 0.0, ActionResult(error="CapSolver solution had nothing usable")
            elapsed = loop.time() - started
            logger.info(
                "solve_captcha: %s solution taskId=%s elapsed=%.1fs cost=%s",
                det.kind, task_id, elapsed, client.parse_cost(result),
            )
            return solution, elapsed, None
        if status == "failed" or result.get("errorId", 0) != 0:
            _record_cost(result, ctx)
            detail = result.get("errorDescription", "unknown")
            await ctx.emit(f"captcha: solve failed ({detail})")
            return None, 0.0, ActionResult(error=f"CapSolver failed: {detail}")

    await ctx.emit("captcha: CapSolver timed out")
    return None, 0.0, ActionResult(error="CapSolver timed out")


async def run_solve(strategy, det: Detection, ctx: SolveContext, giveups: dict[str, int]) -> ActionResult:
    host = ctx.host

    if strategy.unsupported_reason:
        await ctx.emit(f"captcha: {det.kind} cannot be solved here")
        return ActionResult(
            error=f"This page shows {_article(det.kind)} {det.kind} challenge, and "
            f"{strategy.unsupported_reason}. "
            "Nothing was spent. Reach what you need by another path."
        )
    missing = [k for k in strategy.required_params if not det.params.get(k)]
    if missing:
        if det.confidence > 1:
            return ActionResult(
                error=f"The page was recognised as {_article(det.kind)} {det.kind} "
                f"challenge, but its runtime {' and '.join(missing)} parameter"
                f"{'s were' if len(missing) != 1 else ' was'} not available. Nothing "
                "was created and nothing was spent. Wait for the widget to finish "
                "loading, then call solve_captcha once more without clicking or "
                "dragging the challenge."
            )
        return ActionResult(
            error=f"Solving {_article(det.kind)} {det.kind} challenge needs "
            f"{' and '.join(missing)}, "
            "which was not given, so nothing was created and nothing was spent. "
            "Name the field on the page and call solve_captcha again, or reach what "
            "you need by another path."
        )
    if det.interstitial and giveups.get(host, 0) >= 2:
        return ActionResult(
            error=f"Two previous solves on {host} this session did not clear the "
            "challenge, so this one is refused rather than paying for a third "
            "identical attempt. Try a different approach before solving again here."
        )
    cap = getattr(settings, "captcha_cost_cap_usd", 0.0) or 0.0
    if cap and ctx.cost_sink is not None and sum(ctx.cost_sink) >= cap:
        return ActionResult(
            error=f"This run has spent its ${cap:.2f} CAPTCHA ceiling, so no further "
            "solve is attempted. Reach what you need by another path."
        )

    try:
        async with httpx.AsyncClient() as http:
            for attempt in (1, 2):
                extra = await strategy.capture(det, ctx)
                if extra:
                    det = replace(det, params={**det.params, **extra})
                missing = [k for k in strategy.required_params if not det.params.get(k)]
                if missing:
                    return ActionResult(
                        error=(
                            f"The current {det.kind} {' and '.join(missing)} parameter"
                            f"{'s' if len(missing) != 1 else ''} could not be "
                            "refreshed from the page's own challenge request. Nothing "
                            "was created and nothing was spent. Reload the page and call "
                            "solve_captcha again without clicking the widget."
                        )
                    )
                payload = strategy.build_task(det, ctx)

                await ctx.emit(f"captcha: solving {det.kind} on {host}")
                solution, elapsed, err = await _create_and_poll(http, payload, strategy, det, ctx)
                if err is not None:
                    if (
                        attempt == 1
                        and det.kind == "geetest_v3"
                        and _stale_geetest_v3_error(err)
                    ):
                        await ctx.emit(
                            "captcha: stale GeeTest challenge, refreshing from the "
                            "page request (attempt 2 of 2)"
                        )
                        continue
                    return err

                fast = det.interstitial and elapsed < 5
                await ctx.emit(
                    f"captcha: {det.kind} solution arrived in {int(elapsed)}s"
                    + (" (suspiciously fast for a live challenge)" if fast else "")
                )

                try:
                    await strategy.redeem(solution, det, ctx)
                except Exception as e:
                    logger.debug("redeem failed", exc_info=True)
                    return ActionResult(error=f"applying the {det.kind} solution failed: {e}")

                if await strategy.verify(det, ctx):
                    await ctx.emit(f"captcha: cleared on {host}")
                    return ActionResult(
                        extracted_content=f"CAPTCHA cleared: {host} let the page through.",
                        long_term_memory=f"solve_captcha: cleared {det.kind} on {host}",
                    )

                if not det.interstitial:
                    await ctx.emit(f"captcha: {det.kind} solution placed on {host}")
                    return ActionResult(
                        extracted_content=(
                            f"{_article(det.kind).capitalize()} {det.kind} solution is "
                            f"now in the page on {host}. The "
                            "widget may NOT visibly change, because the solution is "
                            "written into the page rather than entered through the "
                            "checkbox, image grid, or slider. Do not judge this by the "
                            "widget's appearance and do not solve again. Submit the form "
                            "now, the way the page expects, then check what it says."
                        ),
                        long_term_memory=(
                            f"solve_captcha: {det.kind} solved on {host}; the widget may "
                            "not visibly change, so submit the form and read the reply"
                        ),
                    )

                if attempt == 1:
                    # @nonobvious(must-hold): the strategy is chosen once, so a page
                    # that re-serves a different challenge type must not have its new
                    # parameters fed to the old builder.
                    fresh = await detect_captcha(ctx.session)
                    if fresh and fresh.kind == det.kind and _fresh_single_use(fresh, det):
                        await ctx.emit("captcha: rejected, retrying with the fresh challenge (attempt 2 of 2)")
                        det = fresh
                        continue

                giveups[host] = giveups.get(host, 0) + 1
                await ctx.emit(f"captcha: {host} still challenging after a solve")
                return ActionResult(
                    error=(
                        f"{_article(det.kind).capitalize()} {det.kind} solution was "
                        f"submitted, but {host} is still "
                        "showing the challenge. Check the parameters the probe collected "
                        "before trying again here, or reach what you need by another path."
                    )
                )
    except httpx.HTTPError as e:
        await ctx.emit(f"captcha: CapSolver unreachable ({e})")
        return ActionResult(error=f"CapSolver HTTP error: {e}")

    return ActionResult(error="captcha solve did not complete")
