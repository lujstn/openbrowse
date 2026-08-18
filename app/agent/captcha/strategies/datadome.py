"""DataDome detection.

The solving service publishes no DataDome task, so a DataDome challenge is
recognised and reported plainly rather than being missed or charged for.
"""

from __future__ import annotations

from app.agent.captcha.base import Detection, TokenStrategy
from app.agent.captcha.registry import register


@register
class DataDome(TokenStrategy):
    kind = "datadome"
    unsupported_reason = "the solving service offers no DataDome task"
    response_fields = ("datadome",)

    def detect(self, probe):
        if probe.get("kind") != "datadome":
            return None
        return Detection(
            kind=self.kind,
            params={"captchaUrl": probe.get("captchaUrl", "")},
            interstitial=bool(probe.get("interstitial")),
            confidence=int(probe.get("confidence", 10)),
        )

    def build_task(self, det, ctx):
        raise NotImplementedError(self.unsupported_reason)
