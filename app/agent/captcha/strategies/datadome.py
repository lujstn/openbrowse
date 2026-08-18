"""DataDome strategy.

DataDome has no proxyless CapSolver variant, so without a configured proxy this
registers only to return an honest capability-gap message from the pipeline.
"""

from __future__ import annotations

from browser_use import BrowserSession

from app.agent.captcha.base import Detection, TokenStrategy
from app.agent.captcha import cdp
from app.agent.captcha.registry import register


@register
class DataDome(TokenStrategy):
    kind = "datadome"
    requires_proxy = True
    solution_keys = ("cookie", "token")

    def detect(self, probe):
        if probe.get("kind") != "datadome":
            return None
        return Detection(
            kind=self.kind,
            params={"captchaUrl": probe.get("captchaUrl", "")},
            interstitial=bool(probe.get("interstitial")),
            confidence=int(probe.get("confidence", 10)),
        )

    def build_task(self, det, ctx):
        return {
            "type": "DatadomeSliderTask",
            "websiteURL": ctx.page_url,
            "captchaUrl": det.params.get("captchaUrl", ""),
            "proxy": ctx.proxy,
        }

    async def _place(self, session: BrowserSession, solution, det):
        cookie = solution.get("cookie")
        if cookie:
            await cdp.set_cookies(session, [{"name": "datadome", "value": str(cookie), "url": det.params.get("captchaUrl") or ""}])
