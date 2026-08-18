"""Cloudflare Turnstile and the Cloudflare Challenge page."""

from __future__ import annotations

import json
from typing import Any

from browser_use import BrowserSession

from app.agent.browser_cdp import _eval_js
from app.agent.captcha.base import Detection, TokenStrategy, _first_present
from app.agent.captcha.registry import register

_PLACE_JS = r"""(function (token) {
  var inputs = document.querySelectorAll('[name="cf-turnstile-response"], #cf-turnstile-response');
  if (!inputs.length) {
    var el = document.createElement("input");
    el.type = "hidden";
    el.name = "cf-turnstile-response";
    var w = document.querySelector(".cf-turnstile");
    (w && w.closest("form") ? w.closest("form") : document.body).appendChild(el);
    inputs = [el];
  }
  for (var i = 0; i < inputs.length; i++) { inputs[i].value = token; }
  try { if (window.turnstile) window.turnstile.getResponse = function () { return token; }; } catch (e) {}
  try {
    var w = document.querySelector('.cf-turnstile[data-callback]');
    var cb = w && w.getAttribute("data-callback");
    if (cb && typeof window[cb] === "function") { window[cb](token); }
  } catch (e) {}
})(%s)"""


@register
class Turnstile(TokenStrategy):
    kind = "turnstile"
    solution_keys = ("token", "gRecaptchaResponse")

    def detect(self, probe):
        if probe.get("kind") != "turnstile":
            return None
        return Detection(
            kind=self.kind,
            params={"siteKey": probe.get("siteKey", "")},
            interstitial=bool(probe.get("interstitial")),
            confidence=int(probe.get("confidence", 10)),
        )

    def build_task(self, det, ctx):
        task: dict[str, Any] = {
            "type": "AntiTurnstileTaskProxyLess",
            "websiteURL": ctx.page_url,
            "websiteKey": det.params.get("siteKey", ""),
        }
        return task

    async def _place(self, session: BrowserSession, solution, det):
        token = _first_present(solution, self.solution_keys) or ""
        await _eval_js(session, _PLACE_JS % json.dumps(token))


@register
class CloudflareChallenge(TokenStrategy):
    kind = "cloudflare_challenge"
    requires_proxy = True
    solution_keys = ("token", "cookies")

    def detect(self, probe):
        return None

    def build_task(self, det, ctx):
        return {
            "type": "AntiCloudflareTask",
            "websiteURL": ctx.page_url,
            "proxy": ctx.proxy,
        }

    async def _place(self, session: BrowserSession, solution, det):
        return None
