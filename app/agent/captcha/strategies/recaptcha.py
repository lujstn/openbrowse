"""reCAPTCHA strategies: v2, v2 enterprise, v3, and the v2 image-grid recognition."""

from __future__ import annotations

import base64
import json
import logging
from typing import Any

from browser_use import BrowserSession

from app.agent.browser_cdp import _eval_js
from app.agent.captcha import cdp
from app.agent.captcha.base import (
    Action,
    Detection,
    RecognitionStrategy,
    SolveContext,
    TokenStrategy,
    _first_present,
)
from app.agent.captcha.registry import register

logger = logging.getLogger(__name__)

_PLACE_JS = r"""(function (token) {
  var out = { fields: 0, inForm: false, valueLen: 0, callback: "", navigated: false };
  var widget = document.querySelector('.g-recaptcha,[data-sitekey]');
  var form = widget && widget.closest ? widget.closest("form") : null;
  if (!form) {
    var anyTa = document.querySelector('textarea[name="g-recaptcha-response"]');
    form = anyTa && anyTa.closest ? anyTa.closest("form") : null;
  }
  var tas = document.querySelectorAll('textarea[name="g-recaptcha-response"], #g-recaptcha-response');
  for (var i = 0; i < tas.length; i++) { tas[i].value = token; tas[i].innerHTML = token; }
  // @nonobvious(forced-by): only fields inside the form element are serialised on
  // submit, so a response box the widget has not rendered yet, or one rendered
  // outside the form, must be replaced by one the submit will actually carry.
  if (form && !form.querySelector('textarea[name="g-recaptcha-response"]')) {
    var ta = document.createElement("textarea");
    ta.name = "g-recaptcha-response";
    ta.id = "g-recaptcha-response";
    ta.style.display = "none";
    ta.value = token;
    ta.innerHTML = token;
    form.appendChild(ta);
  } else if (!form && !tas.length) {
    var loose = document.createElement("textarea");
    loose.name = "g-recaptcha-response";
    loose.id = "g-recaptcha-response";
    loose.style.display = "none";
    loose.value = token;
    document.body.appendChild(loose);
  }
  var all = document.querySelectorAll('textarea[name="g-recaptcha-response"]');
  out.fields = all.length;
  out.valueLen = all.length ? (all[0].value || "").length : 0;
  out.inForm = !!(form && form.querySelector('textarea[name="g-recaptcha-response"]'));
  try {
    var cb = widget && widget.getAttribute("data-callback");
    if (cb && typeof window[cb] === "function") { window[cb](token); out.callback = cb; }
  } catch (e) {}
  try {
    var cfg = window.___grecaptcha_cfg;
    if (cfg && cfg.clients) {
      for (var k in cfg.clients) {
        var c = cfg.clients[k];
        for (var p in c) {
          var o = c[p];
          if (o && typeof o === "object") {
            for (var q in o) {
              var t = o[q];
              if (t && typeof t === "object" && typeof t.callback === "function") {
                try { t.callback(token); out.callback = out.callback || "cfg"; } catch (e) {}
              }
            }
          }
        }
      }
    }
  } catch (e) {}
  return out;
})(%s)"""


def _api_domain(det: Detection) -> str:
    origin = det.params.get("apiOrigin") or ""
    return origin + "/" if origin else ""


class _RecaptchaTokenBase(TokenStrategy):
    solution_keys = ("gRecaptchaResponse", "token")

    async def _place(self, session: BrowserSession, solution, det):
        token = _first_present(solution, self.solution_keys) or ""
        placed = await _eval_js(session, _PLACE_JS % json.dumps(token))
        logger.info(
            "solve_captcha: placed %s token (len=%d fields=%s in_form=%s "
            "written=%s callback=%s)",
            det.kind,
            len(token),
            (placed or {}).get("fields"),
            (placed or {}).get("inForm"),
            (placed or {}).get("valueLen"),
            (placed or {}).get("callback") or "none",
        )


@register
class RecaptchaV2(_RecaptchaTokenBase):
    kind = "recaptcha_v2"

    def detect(self, probe):
        if probe.get("kind") != "recaptcha_v2":
            return None
        return Detection(
            kind=self.kind,
            params={
                "siteKey": probe.get("siteKey", ""),
                "dataS": probe.get("dataS", ""),
                "invisible": bool(probe.get("invisible")),
                "apiOrigin": probe.get("apiOrigin", ""),
            },
            interstitial=bool(probe.get("interstitial")),
            served_host=probe.get("apiOrigin", ""),
            confidence=int(probe.get("confidence", 10)),
        )

    def build_task(self, det, ctx):
        p = det.params
        task: dict[str, Any] = {
            "type": "ReCaptchaV2TaskProxyLess",
            "websiteURL": ctx.page_url,
            "websiteKey": p.get("siteKey", ""),
        }
        if p.get("invisible"):
            task["isInvisible"] = True
        if p.get("dataS"):
            task["recaptchaDataSValue"] = p["dataS"]
        api = _api_domain(det)
        if api:
            task["apiDomain"] = api
        if ctx.cookies:
            task["cookies"] = ctx.cookies
        return task


@register
class RecaptchaV2Enterprise(_RecaptchaTokenBase):
    kind = "recaptcha_v2_enterprise"

    def detect(self, probe):
        if probe.get("kind") != "recaptcha_v2_enterprise":
            return None
        return Detection(
            kind=self.kind,
            params={
                "siteKey": probe.get("siteKey", ""),
                "dataS": probe.get("dataS", ""),
                "invisible": bool(probe.get("invisible")),
                "apiOrigin": probe.get("apiOrigin", ""),
            },
            interstitial=bool(probe.get("interstitial")),
            served_host=probe.get("apiOrigin", ""),
            confidence=int(probe.get("confidence", 10)),
        )

    def build_task(self, det, ctx):
        p = det.params
        task: dict[str, Any] = {
            "type": "ReCaptchaV2EnterpriseTaskProxyLess",
            "websiteURL": ctx.page_url,
            "websiteKey": p.get("siteKey", ""),
        }
        if p.get("invisible"):
            task["isInvisible"] = True
        if p.get("dataS"):
            task["enterprisePayload"] = {"s": p["dataS"]}
        api = _api_domain(det)
        if api:
            task["apiDomain"] = api
        if ctx.cookies:
            task["cookies"] = ctx.cookies
        return task


@register
class RecaptchaV3(_RecaptchaTokenBase):
    kind = "recaptcha_v3"

    def detect(self, probe):
        if probe.get("kind") != "recaptcha_v3":
            return None
        return Detection(
            kind=self.kind,
            params={
                "siteKey": probe.get("siteKey", ""),
                "apiOrigin": probe.get("apiOrigin", ""),
            },
            interstitial=bool(probe.get("interstitial")),
            served_host=probe.get("apiOrigin", ""),
            confidence=int(probe.get("confidence", 10)),
        )

    def build_task(self, det, ctx):
        p = det.params
        task: dict[str, Any] = {
            "type": "ReCaptchaV3TaskProxyLess",
            "websiteURL": ctx.page_url,
            "websiteKey": p.get("siteKey", ""),
            "pageAction": "verify",
            "minScore": 0.9,
        }
        api = _api_domain(det)
        if api:
            task["apiDomain"] = api
        return task


@register
class RecaptchaV2Image(RecognitionStrategy):
    kind = "recaptcha_v2_image"
    priority = 5
    solution_keys = ("objects", "box", "points")
    _WIDGET_SELECTOR = 'iframe[src*="/recaptcha/api2/bframe"]'

    def detect(self, probe):
        # @nonobvious(deliberately-missing): reCAPTCHA image grids are solved
        # server-side by the token task, which returns a token that clears the
        # whole challenge, so the click-the-grid path is never needed here and is
        # kept dormant until a captcha with no token task requires it.
        return None

    async def capture(self, det, ctx):
        box = await cdp.element_box(ctx.session, self._WIDGET_SELECTOR)
        if not box:
            return {}
        metrics = await cdp.viewport_metrics(ctx.session)
        png = await cdp.screenshot_clip(ctx.session, box)
        if not png:
            return {}
        return {
            "image_b64": base64.b64encode(png).decode(),
            "grid_box": box,
            "dpr": metrics.get("dpr", 1),
        }

    def build_task(self, det, ctx):
        return {
            "type": "ReCaptchaV2Classification",
            "image": det.params.get("image_b64", ""),
            "question": det.params.get("question", ""),
        }

    def plan_actions(self, solution, det):
        box = det.params.get("grid_box")
        if not box:
            return []
        cells = _first_present(solution, self.solution_keys) or []
        if not isinstance(cells, list):
            return []
        dpr = float(det.params.get("dpr", 1)) or 1
        return [
            a for a in (self._cell_click(box, dpr, c) for c in cells) if a is not None
        ]

    def _cell_click(self, box, dpr, cell):
        try:
            idx = int(cell)
        except (ValueError, TypeError):
            return None
        cols = 3
        cw = box["width"] / cols
        ch = box["height"] / cols
        row, col = divmod(idx, cols)
        x = box["x"] + cw * (col + 0.5)
        y = box["y"] + ch * (row + 0.5)
        return Action.click(x, y)

    async def _commit(self, session, det):
        await _eval_js(
            session,
            "(function(){var b=document.querySelector('#recaptcha-verify-button');"
            "if(b) b.click();})()",
        )
