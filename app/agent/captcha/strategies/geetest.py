"""Geetest v3 and v4 (multi-field, single-use solutions)."""

from __future__ import annotations

import json
import logging

from browser_use import BrowserSession

from app.agent.browser_cdp import _eval_js
from app.agent.captcha.base import Detection, TokenStrategy
from app.agent.captcha.probe import probe_page
from app.agent.captcha.registry import register

logger = logging.getLogger(__name__)

_PLACE_JS = r"""(function (solution, version, widgetSelector) {
  var mapped = version === 3 ? {
    geetest_challenge: solution.challenge || "",
    geetest_validate: solution.validate || "",
    geetest_seccode: solution.seccode || ""
  } : {
    captcha_id: solution.captcha_id || "",
    lot_number: solution.lot_number || "",
    pass_token: solution.pass_token || "",
    gen_time: solution.gen_time || "",
    captcha_output: solution.captcha_output || ""
  };
  var widget = document.querySelector(widgetSelector);
  var form = widget && widget.closest ? widget.closest("form") : null;
  var fields = 0;
  Object.keys(mapped).forEach(function (name) {
    var selector = '[name="' + name + '"]';
    var elements = document.querySelectorAll(selector);
    if (!elements.length && form) {
      var input = document.createElement("input");
      input.type = "hidden";
      input.name = name;
      form.appendChild(input);
      elements = [input];
    }
    for (var i = 0; i < elements.length; i++) {
      elements[i].value = mapped[name];
      elements[i].dispatchEvent(new Event("input", { bubbles: true }));
      elements[i].dispatchEvent(new Event("change", { bubbles: true }));
      fields++;
    }
  });

  var bridge = window.__openbrowseCaptchaBridge || {};
  var slot = version === 3 ? bridge.geetestV3 : bridge.geetestV4;
  var instance = slot && slot.instance;
  var callbacks = (slot && slot.success ? slot.success.slice() : []);
  if (instance) instance.getValidate = function () { return mapped; };
  for (var c = 0; c < callbacks.length; c++) callbacks[c].call(instance);
  return { fields: fields, callbacks: callbacks.length, instance: !!instance };
})(%s, %s, %s)"""


@register
class GeetestV3(TokenStrategy):
    kind = "geetest_v3"
    required_params = ("gt", "challenge")
    solution_keys = ("challenge", "validate", "seccode")
    response_fields = ("geetest_challenge", "geetest_validate", "geetest_seccode")
    widget_selector = "[data-gt],.geetest_holder,.geetest_wind"

    def detect(self, probe):
        if probe.get("kind") != "geetest_v3":
            return None
        return Detection(
            kind=self.kind,
            params={
                "gt": probe.get("gt", ""),
                "challenge": probe.get("challenge", ""),
                "geetestApiServer": probe.get("geetestApiServer", ""),
            },
            confidence=int(probe.get("confidence", 10)),
        )

    def build_task(self, det, ctx):
        task = {
            "type": "GeeTestTaskProxyLess",
            "websiteURL": ctx.page_url,
            "gt": det.params.get("gt", ""),
            "challenge": det.params.get("challenge", ""),
        }
        if det.params.get("geetestApiServer"):
            task["geetestApiServerSubdomain"] = det.params["geetestApiServer"]
        return task

    async def capture(self, det, ctx):
        fresh = await probe_page(ctx.session)
        if not fresh or fresh.get("kind") != self.kind:
            return {}
        return {
            key: fresh[key]
            for key in ("gt", "challenge", "geetestApiServer")
            if fresh.get(key)
        }

    async def _place(self, session: BrowserSession, solution, det):
        placed = await _eval_js(
            session,
            _PLACE_JS
            % (
                json.dumps(solution),
                3,
                json.dumps(self.widget_selector),
            ),
        ) or {}
        logger.info(
            "solve_captcha: placed geetest_v3 fields=%s callbacks=%s instance=%s",
            placed.get("fields"), placed.get("callbacks"), placed.get("instance"),
        )


@register
class GeetestV4(TokenStrategy):
    kind = "geetest_v4"
    required_params = ("captchaId",)
    solution_keys = (
        "captcha_id", "captcha_output", "gen_time", "lot_number", "pass_token"
    )
    response_fields = (
        "captcha_id", "lot_number", "pass_token", "gen_time", "captcha_output"
    )
    widget_selector = "[data-captcha-id],.geetest_holder,.geetest_wind"

    def detect(self, probe):
        if probe.get("kind") != "geetest_v4":
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
        placed = await _eval_js(
            session,
            _PLACE_JS
            % (
                json.dumps(solution),
                4,
                json.dumps(self.widget_selector),
            ),
        ) or {}
        logger.info(
            "solve_captcha: placed geetest_v4 fields=%s callbacks=%s instance=%s",
            placed.get("fields"), placed.get("callbacks"), placed.get("instance"),
        )
