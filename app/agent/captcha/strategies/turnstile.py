"""Cloudflare Turnstile and the Cloudflare Challenge page."""

from __future__ import annotations

import json
from typing import Any

from browser_use import BrowserSession

from app.agent.browser_cdp import _eval_js
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
