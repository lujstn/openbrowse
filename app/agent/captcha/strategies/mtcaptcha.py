"""MTCaptcha strategy."""

from __future__ import annotations

import json
from typing import Any

from browser_use import BrowserSession

from app.agent.browser_cdp import _eval_js
from app.agent.captcha.base import Detection, TokenStrategy, _first_present
from app.agent.captcha.registry import register

_PLACE_JS = r"""(function (token) {
  var inputs = document.querySelectorAll('[name="mtcaptcha-verifiedtoken"], input.mtcaptcha-verifiedtoken-input');
  for (var i = 0; i < inputs.length; i++) { inputs[i].value = token; }
  try { if (window.mtcaptchaConfig && window.mtcaptchaConfig["verified-callback"]) {
    window.mtcaptchaConfig["verified-callback"]({ verifiedToken: token });
  } } catch (e) {}
})(%s)"""


@register
class MTCaptcha(TokenStrategy):
    kind = "mtcaptcha"
    solution_keys = ("token", "gRecaptchaResponse")

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

    async def _place(self, session: BrowserSession, solution, det):
        token = _first_present(solution, self.solution_keys) or ""
        await _eval_js(session, _PLACE_JS % json.dumps(token))
