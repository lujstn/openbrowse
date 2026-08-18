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

from app.agent.captcha import cdp, client
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


def _fresh_single_use(fresh: Detection, old: Detection) -> bool:
    for k in _SINGLE_USE_KEYS:
        nv = fresh.params.get(k)
        if nv and nv != old.params.get(k):
            return True
    return False


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
            error=f"This page shows a {det.kind} challenge, and {strategy.unsupported_reason}. "
            "Nothing was spent. Reach what you need by another path."
        )
    if strategy.requires_proxy and not ctx.proxy:
        return ActionResult(
            error=f"The {det.kind} challenge type needs an upstream proxy, which is "
            "not configured for this session."
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
            error="The CAPTCHA spend cap for this run has been reached, so no further "
            "solve is attempted. Reach what you need by another path."
        )

    before_url = ctx.page_url
    try:
        async with httpx.AsyncClient() as http:
            for attempt in (1, 2):
                extra = await strategy.capture(det, ctx)
                if extra:
                    det = replace(det, params={**det.params, **extra})
                payload = strategy.build_task(det, ctx)
                if strategy.requires_proxy and ctx.proxy:
                    payload["proxy"] = ctx.proxy

                await ctx.emit(f"captcha: solving {det.kind} on {host}")
                solution, elapsed, err = await _create_and_poll(http, payload, strategy, det, ctx)
                if err is not None:
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

                if await strategy.verify(det, ctx, before_url):
                    await ctx.emit(f"captcha: cleared on {host}")
                    return ActionResult(
                        extracted_content=f"CAPTCHA cleared: {host} let the page through.",
                        long_term_memory=f"solve_captcha: cleared {det.kind} on {host}",
                    )

                if not det.interstitial:
                    await ctx.emit(f"captcha: {det.kind} solution placed on {host}")
                    return ActionResult(
                        extracted_content=(
                            f"A {det.kind} solution is now in the page on {host}. The "
                            "checkbox will NOT visibly tick, because the solution is "
                            "written straight into the page rather than by clicking it, "
                            "so do not judge this by the widget's appearance and do not "
                            "solve again. Submit the form now, the way the page expects, "
                            "then check what the page says in reply."
                        ),
                        long_term_memory=(
                            f"solve_captcha: {det.kind} solved on {host}; the widget will "
                            "not look ticked, so submit the form and read the reply"
                        ),
                    )

                if attempt == 1:
                    fresh = await detect_captcha(ctx.session)
                    if fresh and _fresh_single_use(fresh, det):
                        await ctx.emit("captcha: rejected, retrying with the fresh challenge (attempt 2 of 2)")
                        det = fresh
                        continue

                giveups[host] = giveups.get(host, 0) + 1
                await ctx.emit(f"captcha: {host} still challenging after a solve")
                return ActionResult(
                    error=(
                        f"A {det.kind} solution was submitted, but {host} is still "
                        "showing the challenge. Check the parameters the probe collected "
                        "before trying again here, or reach what you need by another path."
                    )
                )
    except httpx.HTTPError as e:
        await ctx.emit(f"captcha: CapSolver unreachable ({e})")
        return ActionResult(error=f"CapSolver HTTP error: {e}")

    return ActionResult(error="captcha solve did not complete")
