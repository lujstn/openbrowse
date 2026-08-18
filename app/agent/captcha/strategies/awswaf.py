"""AWS WAF strategies: the token task and the image-recognition task."""

from __future__ import annotations

import base64
from typing import Any

from browser_use import BrowserSession

from app.agent.browser_cdp import _eval_js
from app.agent.captcha import cdp
from app.agent.captcha.base import (
    Action,
    Detection,
    RecognitionStrategy,
    TokenStrategy,
    _first_present,
)
from app.agent.captcha.registry import register


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

    async def _place(self, session: BrowserSession, solution, det):
        token = _first_present(solution, self.solution_keys) or ""
        await _eval_js(
            session,
            "(function(t){try{window.awsWafCookieDomainList;"
            "document.cookie='aws-waf-token='+t+';path=/';}catch(e){}})(%r)" % str(token),
        )


@register
class AwsWafImage(RecognitionStrategy):
    kind = "awswaf_image"
    priority = 5
    solution_keys = ("box", "objects", "points")
    _WIDGET_SELECTOR = 'iframe[src*="captcha"], #captcha-container'

    def detect(self, probe):
        if probe.get("kind") != "awswaf_image":
            return None
        return Detection(
            kind=self.kind,
            params={"question": probe.get("question", "")},
            interstitial=bool(probe.get("interstitial")),
            confidence=int(probe.get("confidence", 10)) + 5,
        )

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
