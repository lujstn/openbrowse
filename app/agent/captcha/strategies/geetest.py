"""Geetest v3 and v4 (multi-field, single-use solutions)."""

from __future__ import annotations

import json

from browser_use import BrowserSession

from app.agent.browser_cdp import _eval_js
from app.agent.captcha.base import Detection, TokenStrategy
from app.agent.captcha.registry import register

_PLACE_JS = r"""(function (solution) {
  try { window.__capsolver_geetest = solution; } catch (e) {}
  try {
    var cb = window.geetestCallback || window.onGeetestValidate;
    if (typeof cb === "function") { cb(solution); }
  } catch (e) {}
})(%s)"""


@register
class GeetestV3(TokenStrategy):
    kind = "geetest_v3"
    solution_keys = ("challenge", "validate", "seccode")

    def detect(self, probe):
        if probe.get("kind") != "geetest_v3":
            return None
        if not probe.get("gt"):
            return None
        return Detection(
            kind=self.kind,
            params={"gt": probe.get("gt", ""), "challenge": probe.get("challenge", "")},
            confidence=int(probe.get("confidence", 10)),
        )

    def build_task(self, det, ctx):
        return {
            "type": "GeeTestTaskProxyLess",
            "websiteURL": ctx.page_url,
            "gt": det.params.get("gt", ""),
            "challenge": det.params.get("challenge", ""),
        }

    async def _place(self, session: BrowserSession, solution, det):
        await _eval_js(session, _PLACE_JS % json.dumps(solution))


@register
class GeetestV4(TokenStrategy):
    kind = "geetest_v4"
    solution_keys = ("captcha_id", "captcha_output", "gen_time", "lot_number", "pass_token")

    def detect(self, probe):
        if probe.get("kind") != "geetest_v4":
            return None
        if not probe.get("captchaId"):
            return None
        return Detection(
            kind=self.kind,
            params={"captchaId": probe.get("captchaId", "")},
            confidence=int(probe.get("confidence", 10)),
        )

    def build_task(self, det, ctx):
        return {
            "type": "GeeTestTaskProxyLess",
            "websiteURL": ctx.page_url,
            "captchaId": det.params.get("captchaId", ""),
        }

    async def _place(self, session: BrowserSession, solution, det):
        await _eval_js(session, _PLACE_JS % json.dumps(solution))
