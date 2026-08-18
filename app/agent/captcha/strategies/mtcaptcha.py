"""MTCaptcha strategy."""

from __future__ import annotations

import json
from typing import Any

from browser_use import BrowserSession

from app.agent.browser_cdp import _eval_js
from app.agent.captcha.base import Detection, TokenStrategy, _first_present
from app.agent.captcha.registry import register


@register
class MTCaptcha(TokenStrategy):
    kind = "mtcaptcha"
    solution_keys = ("token", "gRecaptchaResponse")
    response_fields = ("mtcaptcha-verifiedtoken",)
    widget_selector = ".mtcaptcha,[data-mtcaptcha-sitekey],#mtcaptcha"
    response_tag = "input"

    def detect(self, probe):
        if probe.get("kind") != "mtcaptcha":
            return None
        return Detection(
            kind=self.kind,
            params={"siteKey": probe.get("siteKey", "")},
            interstitial=bool(probe.get("interstitial")),
            confidence=int(probe.get("confidence", 10)),
        )

    def build_task(self, det, ctx):
        task: dict[str, Any] = {
            "type": "MtCaptchaTaskProxyLess",
            "websiteURL": ctx.page_url,
            "websiteKey": det.params.get("siteKey", ""),
        }
        return task

    async def _after_place(self, session: BrowserSession, token, det):
        await _eval_js(
            session,
            "(function(t){try{var c=window.mtcaptchaConfig;"
            "if(c&&c['verified-callback'])c['verified-callback']({verifiedToken:t});}"
            "catch(e){}})(%s)" % json.dumps(token),
        )
