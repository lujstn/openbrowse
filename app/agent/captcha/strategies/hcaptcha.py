"""hCaptcha strategy."""

from __future__ import annotations

import json
from typing import Any

from browser_use import BrowserSession

from app.agent.browser_cdp import _eval_js
from app.agent.captcha.base import Detection, TokenStrategy, _first_present
from app.agent.captcha.registry import register

_PLACE_JS = r"""(function (token) {
  var tas = document.querySelectorAll('textarea[name="h-captcha-response"], textarea[name="g-recaptcha-response"]');
  if (!tas.length) {
    var ta = document.createElement("textarea");
    ta.name = "h-captcha-response";
    ta.style.display = "none";
    var form = document.querySelector(".h-captcha") ;
    (form && form.closest("form") ? form.closest("form") : document.body).appendChild(ta);
    tas = [ta];
  }
  for (var i = 0; i < tas.length; i++) { tas[i].value = token; tas[i].innerHTML = token; }
  try {
    var w = document.querySelector('.h-captcha[data-callback],[data-hcaptcha-sitekey][data-callback]');
    var cb = w && w.getAttribute("data-callback");
    if (cb && typeof window[cb] === "function") { window[cb](token); }
  } catch (e) {}
})(%s)"""


@register
class HCaptcha(TokenStrategy):
    kind = "hcaptcha"
    solution_keys = ("gRecaptchaResponse", "token")

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

    async def _place(self, session: BrowserSession, solution, det):
        token = _first_present(solution, self.solution_keys) or ""
        await _eval_js(session, _PLACE_JS % json.dumps(token))
