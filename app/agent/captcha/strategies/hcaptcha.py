"""hCaptcha strategy."""

from __future__ import annotations

from typing import Any

from app.agent.captcha.base import Detection, TokenStrategy
from app.agent.captcha.registry import register


@register
class HCaptcha(TokenStrategy):
    kind = "hcaptcha"
    solution_keys = ("gRecaptchaResponse", "token")
    response_fields = ("h-captcha-response", "g-recaptcha-response")
    widget_selector = ".h-captcha,[data-hcaptcha-sitekey]"

    def detect(self, probe):
        if probe.get("kind") != "hcaptcha":
            return None
        return Detection(
            kind=self.kind,
            params={
                "siteKey": probe.get("siteKey", ""),
                "invisible": bool(probe.get("invisible")),
            },
            interstitial=bool(probe.get("interstitial")),
            confidence=int(probe.get("confidence", 10)),
        )

    def build_task(self, det, ctx):
        p = det.params
        task: dict[str, Any] = {
            "type": "HCaptchaTaskProxyLess",
            "websiteURL": ctx.page_url,
            "websiteKey": p.get("siteKey", ""),
        }
        if p.get("invisible"):
            task["isInvisible"] = True
        return task

