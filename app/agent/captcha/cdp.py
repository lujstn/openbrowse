"""CDP page-interaction primitives for the captcha subsystem.

Token strategies need only page_advanced and submit_widget_form; recognition
strategies also need screenshots, coordinate clicks, text entry and box geometry.
All are host-agnostic.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from browser_use import BrowserSession

from app.agent.browser_cdp import _eval_js
from app.agent.captcha.probe import probe_strict

logger = logging.getLogger(__name__)

_SUBMIT_WIDGET_JS = r"""(function () {
  var w = document.querySelector('.g-recaptcha,[data-sitekey],.h-captcha,.cf-turnstile,[data-mtcaptcha-sitekey]');
  var f = w && w.closest ? w.closest("form") : null;
  if (!f) f = document.querySelector("form");
  if (!f) return false;
  if (f.requestSubmit) f.requestSubmit(); else f.submit();
  return true;
})()"""


async def page_advanced(
    browser_session: BrowserSession, before_url: str, strategy: Any = None,
    timeout_s: float = 25.0
) -> bool:
    """Whether the challenge actually let us through.

    Judged by the page moving on or the challenge no longer being present, never
    by a token landing in a field.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        await asyncio.sleep(1.0)
        try:
            now_url = await _eval_js(browser_session, "window.location.href") or ""
        except Exception:
            now_url = ""
        if now_url and now_url != before_url:
            return True
        try:
            if await probe_strict(browser_session) is None:
                return True
        except Exception:
            logger.debug("captcha re-check failed; retrying", exc_info=True)
    return False


async def submit_widget_form(browser_session: BrowserSession) -> None:
    """Submit the form that contains the captcha widget, if any.

    @nonobvious(must-hold): the widget callback sometimes navigates on its own, so
    a short wait lets that settle; a form already gone leaves nothing to match and
    this becomes a no-op rather than a double submit.
    """
    await asyncio.sleep(0.8)
    try:
        await _eval_js(browser_session, _SUBMIT_WIDGET_JS)
    except Exception:
        logger.debug("submit_widget_form failed", exc_info=True)


async def click_coordinate(
    browser_session: BrowserSession, x: float, y: float
) -> None:
    cdp = await browser_session.get_or_create_cdp_session()
    for etype in ("mousePressed", "mouseReleased"):
        await cdp.cdp_client.send.Input.dispatchMouseEvent(
            params={
                "type": etype, "x": float(x), "y": float(y),
                "button": "left", "clickCount": 1,
            },
            session_id=cdp.session_id,
        )
    await asyncio.sleep(0.25)


async def type_text(
    browser_session: BrowserSession, selector: str, text: str
) -> None:
    await _eval_js(
        browser_session,
        "(function(){var e=document.querySelector(%s);"
        "if(e){e.focus();try{e.value='';}catch(x){}}})()" % json.dumps(selector),
    )
    cdp = await browser_session.get_or_create_cdp_session()
    await cdp.cdp_client.send.Input.insertText(
        params={"text": text}, session_id=cdp.session_id
    )


async def element_box(
    browser_session: BrowserSession, selector: str
) -> dict[str, float] | None:
    return await _eval_js(
        browser_session,
        "(function(){var e=document.querySelector(%s); if(!e) return null;"
        "var b=e.getBoundingClientRect();"
        "return {x:b.x,y:b.y,width:b.width,height:b.height};})()" % json.dumps(selector),
    )


async def viewport_metrics(browser_session: BrowserSession) -> dict[str, float]:
    m = await _eval_js(
        browser_session,
        "({dpr:window.devicePixelRatio||1,scrollX:window.scrollX,scrollY:window.scrollY})",
    )
    return m if isinstance(m, dict) else {"dpr": 1, "scrollX": 0, "scrollY": 0}


async def screenshot_clip(
    browser_session: BrowserSession, box: dict[str, float]
) -> bytes | None:
    clip = {
        "x": float(box["x"]), "y": float(box["y"]),
        "width": float(box["width"]), "height": float(box["height"]),
    }
    try:
        return await browser_session.take_screenshot(clip=clip, format="png")
    except Exception:
        logger.debug("screenshot_clip failed", exc_info=True)
        return None


def cookie_header_for(cookies: list[dict[str, Any]], host: str) -> str:
    """The page's own cookies as a ``name=value;…`` header, scoped to its host."""
    host = (host or "").lower()
    if ":" in host:
        host = host.rsplit(":", 1)[0]
    if not host:
        return ""
    parts: list[str] = []
    seen: set[str] = set()
    for c in cookies:
        domain = str(c.get("domain") or "").lstrip(".").lower()
        name = str(c.get("name") or "")
        if not domain or not name or name in seen:
            continue
        if host != domain and not host.endswith("." + domain):
            continue
        seen.add(name)
        parts.append(f"{name}={c.get('value') or ''}")
    return ";".join(parts)


async def page_cookie_header(browser_session: BrowserSession, host: str) -> str:
    try:
        jar = await browser_session._cdp_get_cookies()
    except Exception:
        logger.debug("reading cookies for the captcha task failed", exc_info=True)
        return ""
    return cookie_header_for([dict(c) for c in (jar or [])], host)


async def set_cookies(
    browser_session: BrowserSession, cookies: list[dict[str, Any]]
) -> None:
    cdp = await browser_session.get_or_create_cdp_session()
    for c in cookies:
        try:
            await cdp.cdp_client.send.Network.setCookie(
                params=c, session_id=cdp.session_id
            )
        except Exception:
            logger.debug("set_cookies entry failed", exc_info=True)
