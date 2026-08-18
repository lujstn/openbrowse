"""Generic image recognition: ImageToText and VisionEngine.

A bare image challenge has no universal DOM marker, so these do not auto-detect
from a page probe; they are driven by an explicit hint (kind + the answer field
selector) and unit-tested through build_task and plan_actions.
"""

from __future__ import annotations

import base64

from app.agent.captcha import cdp
from app.agent.captcha.base import Action, Detection, RecognitionStrategy, _first_present
from app.agent.captcha.registry import register


class _ImageAnswerStrategy(RecognitionStrategy):
    solution_keys = ("text",)
    module = ""

    async def capture(self, det, ctx):
        selector = det.params.get("image_selector") or "img"
        box = await cdp.element_box(ctx.session, selector)
        if not box:
            return {}
        png = await cdp.screenshot_clip(ctx.session, box)
        if not png:
            return {}
        return {"image_b64": base64.b64encode(png).decode()}

    def build_task(self, det, ctx):
        task = {"type": "ImageToTextTask", "body": det.params.get("image_b64", "")}
        if self.module:
            task["module"] = self.module
        return task

    def plan_actions(self, solution, det):
        text = _first_present(solution, self.solution_keys)
        field = det.params.get("answer_selector")
        if not text or not field:
            return []
        return [Action.type_into(field, str(text))]


@register
class ImageToText(_ImageAnswerStrategy):
    kind = "imagetotext"

    def detect(self, probe):
        return None

