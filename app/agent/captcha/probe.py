"""Host-agnostic captcha page probe.

One in-page sweep harvests structural facts about whatever challenge is present.
Strategies classify from that snapshot in Python. No target host, no site name
and no url-path literal appears here: an interstitial is recognised by structure
and the captcha's serving origin is read off the page, never hardcoded.
"""

from __future__ import annotations

import logging
from typing import Any

from browser_use import BrowserSession

from app.agent.browser_cdp import _eval_js

logger = logging.getLogger(__name__)

_PROBE_JS = r"""(function () {
  function attr(el, names) {
    for (var i = 0; i < names.length; i++) {
      var v = el && el.getAttribute(names[i]);
      if (v) return v;
    }
    return "";
  }
  function frameParam(part, name) {
    var f = document.querySelector('iframe[src*="' + part + '"]');
    if (!f) return "";
    try {
      return new URL(f.getAttribute("src"), location.href).searchParams.get(name) || "";
    } catch (e) { return ""; }
  }
  function originOf(sel) {
    var el = document.querySelector(sel);
    if (!el) return "";
    var src = el.getAttribute("src") || "";
    try { return new URL(src, location.href).origin; } catch (e) { return ""; }
  }
  function runtimeParam(names) {
    var urls = [];
    var scripts = document.querySelectorAll("script[src]");
    for (var i = 0; i < scripts.length; i++) urls.push(scripts[i].src || "");
    try {
      var entries = performance.getEntriesByType("resource");
      for (var j = 0; j < entries.length; j++) urls.push(entries[j].name || "");
    } catch (e) {}
    for (var u = urls.length - 1; u >= 0; u--) {
      try {
        if (!/(^|[.\/])(gee(test|visit)|gcaptcha4)[.\/]/i.test(urls[u])) continue;
        var params = new URL(urls[u], location.href).searchParams;
        for (var n = 0; n < names.length; n++) {
          var value = params.get(names[n]);
          if (value) return value;
        }
      } catch (e) {}
    }
    return "";
  }
  function bridgeSlot(name) {
    var bridge = window.__openbrowseCaptchaBridge;
    return (bridge && bridge[name]) || {};
  }
  function mtConfiguration() {
    var config = window.mtcaptchaConfig || {};
    try {
      if (window.mtcaptcha && typeof window.mtcaptcha.getConfiguration === "function") {
        var live = window.mtcaptcha.getConfiguration();
        if (live && live.sitekey) config = live;
      }
    } catch (e) {}
    return config || {};
  }
  // @nonobvious(forced-by): a score-based token is minted against the action name
  // the page passes to its own execute() call and is rejected under any other, so
  // the name has to be read off the page rather than assumed.
  function scoreAction() {
    var el = document.querySelector(".g-recaptcha[data-action],[data-sitekey][data-action]");
    var a = el && el.getAttribute("data-action");
    if (a) return a;
    var re = /grecaptcha[\s\S]{0,200}?execute\s*\(\s*[^)]{0,160}?action\s*:\s*['"]([\w .\/-]{1,64})['"]/;
    var inline = document.querySelectorAll("script:not([src])");
    for (var i = 0; i < inline.length; i++) {
      var m = re.exec(inline[i].textContent || "");
      if (m) return m[1];
    }
    return "";
  }
  function isInterstitial(widget) {
    if (!widget) return false;
    var form = widget.closest ? widget.closest("form") : null;
    if (!form) return false;
    var creds = form.querySelectorAll(
      'input[type="text"],input[type="password"],input[type="email"],' +
      'input[type="tel"],input[type="search"],input:not([type]),textarea'
    );
    for (var i = 0; i < creds.length; i++) {
      var s = window.getComputedStyle(creds[i]);
      if (s && s.display !== "none" && s.visibility !== "hidden") return false;
    }
    return true;
  }

  var out = {
    kind: "", siteKey: "", dataS: "", invisible: false, interstitial: false,
    apiOrigin: "", question: "", captchaId: "", gt: "", challenge: "",
    captchaUrl: "", action: "", confidence: 0
  };

  var rc = document.querySelector(".g-recaptcha,[data-sitekey]");
  var hc = document.querySelector(".h-captcha,[data-hcaptcha-sitekey]");
  var ts = document.querySelector(".cf-turnstile,[data-cf-turnstile-sitekey]");
  var gtEl = document.querySelector("[data-gt],[data-captcha-id],.geetest_holder,.geetest_wind");
  var mt = document.querySelector(".mtcaptcha,[data-mtcaptcha-sitekey],#mtcaptcha");

  if (ts || document.querySelector('iframe[src*="challenges.cloudflare.com"]')) {
    out.kind = "turnstile";
    out.siteKey = attr(ts, ["data-sitekey", "data-cf-turnstile-sitekey"]);
    out.confidence = ts ? 20 : 8;
  } else if (hc || document.querySelector('iframe[src*="hcaptcha.com"]')) {
    out.kind = "hcaptcha";
    out.siteKey = attr(hc, ["data-sitekey", "data-hcaptcha-sitekey"]) ||
      frameParam("hcaptcha.com", "sitekey");
    out.invisible = attr(hc, ["data-size"]) === "invisible";
    out.confidence = hc ? 20 : 8;
  } else if (gtEl || bridgeSlot("geetestV3").config || bridgeSlot("geetestV4").config) {
    var gt3 = bridgeSlot("geetestV3").config || {};
    var gt4 = bridgeSlot("geetestV4").config || {};
    out.captchaId = gt4.captchaId || gt4.captcha_id ||
      attr(gtEl, ["data-captcha-id"]) || runtimeParam(["captcha_id", "captchaId"]);
    out.gt = gt3.gt || attr(gtEl, ["data-gt"]) || runtimeParam(["gt"]);
    out.challenge = runtimeParam(["challenge"]) || gt3.challenge ||
      attr(gtEl, ["data-challenge"]);
    out.geetestApiServer = gt3.api_server || gt3.apiServer ||
      runtimeParam(["api_server"]);
    out.kind = out.captchaId ? "geetest_v4" : "geetest_v3";
    out.confidence = 15;
  } else if (mt) {
    var mtConfig = mtConfiguration();
    out.kind = "mtcaptcha";
    out.siteKey = mtConfig.sitekey ||
      attr(mt, ["data-mtcaptcha-sitekey", "data-sitekey"]);
    out.confidence = 15;
  } else if (document.querySelector('iframe[src*="captcha-delivery.com"]')) {
    out.kind = "datadome";
    var dd = document.querySelector('iframe[src*="captcha-delivery.com"]');
    out.captchaUrl = (dd && dd.getAttribute("src")) || "";
    out.confidence = 15;
  } else if (document.querySelector('script[src*="awswaf"],[id*="awswaf"],iframe[title*="AWS WAF"]')) {
    out.kind = "awswaf_token";
    out.confidence = 12;
  } else if (rc || document.querySelector('iframe[src*="/recaptcha/api2/anchor"]')) {
    out.kind = "recaptcha_v2";
    out.siteKey = attr(rc, ["data-sitekey"]) || frameParam("recaptcha/api2/anchor", "k");
    out.dataS = attr(rc, ["data-s"]) || frameParam("recaptcha/api2/anchor", "s");
    out.invisible = attr(rc, ["data-size"]) === "invisible";
    if (attr(rc, ["data-enterprise"]) === "true") out.kind = "recaptcha_v2_enterprise";
    out.apiOrigin = originOf('iframe[src*="/recaptcha/"]') || originOf('script[src*="/recaptcha/"]');
    out.confidence = rc ? 20 : 8;
    if (document.querySelector('iframe[src*="/recaptcha/api2/bframe"]')) {
      var vis = document.querySelector('iframe[src*="/recaptcha/api2/bframe"]');
      try {
        var st = window.getComputedStyle(vis.closest('div[style]') || vis);
        if (st && st.visibility !== "hidden" && st.opacity !== "0") {
          out.question = (document.querySelector(".rc-imageselect-desc-no-canonical,.rc-imageselect-desc") || {}).innerText || "";
        }
      } catch (e) {}
    }
  } else {
    var ent = document.querySelector('script[src*="/recaptcha/enterprise.js?render="]');
    var v3 = ent || document.querySelector('script[src*="/recaptcha/api.js?render="]');
    if (v3) {
      out.kind = ent ? "recaptcha_v3_enterprise" : "recaptcha_v3";
      try {
        out.siteKey = new URL(v3.getAttribute("src"), location.href).searchParams.get("render") || "";
      } catch (e) {}
      out.action = scoreAction();
      out.apiOrigin = originOf('script[src*="/recaptcha/"]');
      out.confidence = 12;
    }
  }

  var widget = rc || hc || ts || gtEl || mt;
  out.interstitial = isInterstitial(widget);
  if (!out.apiOrigin) {
    out.apiOrigin = originOf('iframe[src*="/recaptcha/"]') || originOf('script[src*="/recaptcha/"]');
  }
  return out.kind ? out : null;
})()"""


async def probe_strict(browser_session: BrowserSession) -> dict[str, Any] | None:
    """Run the page probe, letting a failed evaluation raise.

    @nonobvious(must-hold): "the check failed" and "there is no challenge" must
    stay tellable apart. Evaluating a page mid-navigation raises, which is the
    ordinary case while an interstitial submits, and reading that as an all-clear
    would report a solve that never happened.
    """
    found = await _eval_js(browser_session, _PROBE_JS)
    if not isinstance(found, dict) or not found.get("kind"):
        return None
    return found


async def probe_page(browser_session: BrowserSession) -> dict[str, Any] | None:
    """What challenge the current page shows, or None if it cannot be told."""
    try:
        return await probe_strict(browser_session)
    except Exception:
        logger.debug("probe_page failed", exc_info=True)
        return None
