"""hCaptcha detection.

The solving service publishes no hCaptcha task, so an hCaptcha is recognised and
reported plainly rather than being missed or charged for.
"""

from __future__ import annotations

from openbrowse.agent.captcha.base import Detection, TokenStrategy
from openbrowse.agent.captcha.registry import register


@register
class HCaptcha(TokenStrategy):
    kind = "hcaptcha"
    unsupported_reason = "the solving service offers no hCaptcha task"
    response_fields = ("h-captcha-response",)
    widget_selector = ".h-captcha,[data-hcaptcha-sitekey]"

    def detect(self, probe):
        if probe.get("kind") != "hcaptcha":
            return None
        return Detection(
            kind=self.kind,
            params={"siteKey": probe.get("siteKey", "")},
            interstitial=bool(probe.get("interstitial")),
            confidence=int(probe.get("confidence", 10)),
        )

    def build_task(self, det, ctx):
        raise NotImplementedError(self.unsupported_reason)
