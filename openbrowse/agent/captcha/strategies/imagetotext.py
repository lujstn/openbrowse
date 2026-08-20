"""Generic image recognition: read the answer out of a picture and type it.

A bare image challenge has no universal DOM marker, so these do not auto-detect
from a page probe. They are driven by explicit hints on solve_captcha: the answer
field's selector, without which nothing can be typed, and optionally the image's
selector when the first image on the page is not the challenge.
"""

from __future__ import annotations

import base64

from openbrowse.agent.captcha import cdp
from openbrowse.agent.captcha.base import Action, RecognitionStrategy, _first_present
from openbrowse.agent.captcha.registry import register


class _ImageAnswerStrategy(RecognitionStrategy):
    solution_keys = ("text",)
    required_params = ("answer_selector",)
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
        # @nonobvious(deliberately-missing): a bare image carries no marker that tells
        # a challenge from an ordinary picture, so this is reached by hint only.
        return None

