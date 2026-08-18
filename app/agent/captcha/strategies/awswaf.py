"""AWS WAF strategies: the token task and the image-recognition task."""

from __future__ import annotations

import base64

from app.agent.captcha import cdp
from app.agent.captcha.base import (
    Action,
    Detection,
    RecognitionStrategy,
    TokenStrategy,
    _first_present,
)
from app.agent.captcha.registry import register

_COOKIE_NAME = "aws-waf-token"


@register
class AwsWafToken(TokenStrategy):
    kind = "awswaf_token"
    solution_keys = ("token", "cookie")

    def detect(self, probe):
        if probe.get("kind") != "awswaf_token":
            return None
        return Detection(
            kind=self.kind,
            params={},
            interstitial=bool(probe.get("interstitial")),
            confidence=int(probe.get("confidence", 10)),
        )

    def build_task(self, det, ctx):
        return {"type": "AntiAwsWafTaskProxyLess", "websiteURL": ctx.page_url}

    async def redeem(self, solution, det, ctx):
        # @nonobvious(forced-by): this interstitial has no form to post, and the token
        # only counts as a cookie on the next request, so the page is re-requested
        # rather than submitted.
        raw = str(_first_present(solution, self.solution_keys) or "")
        value = raw.split("=", 1)[1] if raw.startswith(_COOKIE_NAME + "=") else raw
        if not value:
            raise ValueError("the solution carried no token")
        await cdp.set_cookies(
            ctx.session, cdp.normalise_cookies({_COOKIE_NAME: value}, ctx.host)
        )
        await cdp.reload_page(ctx.session)


@register
class AwsWafImage(RecognitionStrategy):
    kind = "awswaf_image"
    priority = 5
    unsupported_reason = (
        "the image-grid path for it has never been proven against a live challenge, "
        "so it is not offered"
    )
    solution_keys = ("box", "objects", "points")
    _WIDGET_SELECTOR = 'iframe[src*="captcha"], #captcha-container'

    def detect(self, probe):
        # @nonobvious(deliberately-missing): the page probe has no marker that tells
        # a WAF image grid from the token interstitial, so this never fires from a
        # probe; it is kept whole for the day one is found.
        return None

    async def capture(self, det, ctx):
        box = await cdp.element_box(ctx.session, self._WIDGET_SELECTOR)
        if not box:
            return {}
        metrics = await cdp.viewport_metrics(ctx.session)
        png = await cdp.screenshot_clip(ctx.session, box)
        if not png:
            return {}
        return {
            "image_b64": base64.b64encode(png).decode(),
            "grid_box": box,
            "dpr": metrics.get("dpr", 1),
        }

    def build_task(self, det, ctx):
        return {
            "type": "AwsWafClassification",
            "images": [det.params.get("image_b64", "")],
            "question": det.params.get("question", ""),
        }

    def plan_actions(self, solution, det):
        box = det.params.get("grid_box")
        points = _first_present(solution, self.solution_keys)
        if not box or not isinstance(points, list):
            return []
        dpr = float(det.params.get("dpr", 1)) or 1
        actions: list[Action] = []
        for p in points:
            if isinstance(p, dict) and "x" in p and "y" in p:
                x = box["x"] + float(p["x"]) / dpr
                y = box["y"] + float(p["y"]) / dpr
                actions.append(Action.click(x, y))
        return actions
