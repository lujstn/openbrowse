"""Cloudflare Turnstile and the Cloudflare Challenge page."""

from __future__ import annotations

import json
from typing import Any

from browser_use import BrowserSession

from app.agent.browser_cdp import _eval_js
from app.agent.captcha import cdp
from app.agent.captcha.base import Detection, TokenStrategy
from app.agent.captcha.registry import register


@register
class Turnstile(TokenStrategy):
    kind = "turnstile"
    solution_keys = ("token", "gRecaptchaResponse")
    response_fields = ("cf-turnstile-response",)
    widget_selector = ".cf-turnstile,[data-cf-turnstile-sitekey]"
    response_tag = "input"

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

    async def _after_place(self, session: BrowserSession, token, det):
        await _eval_js(
            session,
            "(function(t){try{if(window.turnstile)"
            "window.turnstile.getResponse=function(){return t;};}catch(e){}})(%s)"
            % json.dumps(token),
        )


@register
class CloudflareChallenge(TokenStrategy):
    """The interstitial Cloudflare serves in place of the page, cleared by cookie."""

    kind = "cloudflare_challenge"
    requires_proxy = True
    solution_keys = ("token", "cookies")

    def detect(self, probe):
        # @nonobvious(deliberately-missing): the challenge page carries no marker the
        # probe can tell apart from an ordinary Turnstile widget, so this is reached
        # by an explicit hint rather than by detection.
        return None

    def build_task(self, det, ctx):
        # @nonobvious(forced-by): the service refuses any anticloudflare task without
        # metadata, and the challenge variant is named there rather than by task type.
        return {
            "type": "AntiCloudflareTask",
            "websiteURL": ctx.page_url,
            "websiteKey": det.params.get("siteKey", ""),
            "proxy": ctx.proxy,
            "metadata": {"type": "challenge"},
        }

    async def redeem(self, solution, det, ctx):
        # @nonobvious(forced-by): the answer here is a clearance cookie rather than a
        # field value, and a cookie is only honoured on a fresh request, so it must be
        # written into the jar and the page re-requested.
        cookies = cdp.normalise_cookies(solution.get("cookies"), ctx.host)
        if not cookies:
            raise ValueError("the solution carried no clearance cookies")
        await cdp.set_cookies(ctx.session, cookies)
        await cdp.reload_page(ctx.session)
