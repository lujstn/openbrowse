"""Custom browser-use tools — Capsolver CAPTCHA solving, Python sandbox, HTTP fetch."""

import json
import logging
import re
from typing import Any

import httpx
from browser_use import ActionResult, BrowserSession, Tools

from app.config import settings

logger = logging.getLogger(__name__)

CAPSOLVER_API = "https://api.capsolver.com"


async def _eval_js(browser_session: BrowserSession, expression: str) -> Any:
    """Execute JavaScript via BrowserSession's CDP connection.

    @nonobvious(forced-by): browser-use 0.13.7's CDP client only supports the typed
    ``send.Runtime.evaluate(params=..., session_id=...)`` form via a per-target
    ``get_or_create_cdp_session()``, not ``send(method, params)``.
    """
    cdp_session = await browser_session.get_or_create_cdp_session()
    result = await cdp_session.cdp_client.send.Runtime.evaluate(
        params={"expression": expression, "returnByValue": True, "awaitPromise": True},
        session_id=cdp_session.session_id,
    )
    if result.get("exceptionDetails"):
        raise RuntimeError(f"JS error: {result['exceptionDetails']}")
    return result.get("result", {}).get("value")


class _SandboxBrowser:
    """Read-side browser bridge exposed to the code sandbox (like the cloud's
    ``browser`` handle): JavaScript eval and DOM access against the live page.
    """

    def __init__(self, session: BrowserSession) -> None:
        self._session = session

    async def evaluate(self, js: str) -> Any:
        return await _eval_js(self._session, js)

    async def get_html(self, selector: str | None = None) -> str:
        if selector:
            js = (
                "(function(){var el=document.querySelector("
                + json.dumps(selector)
                + ");return el?el.outerHTML:''})()"
            )
        else:
            js = "document.documentElement.outerHTML"
        return await _eval_js(self._session, js) or ""

    async def navigate(self, url: str) -> None:
        import asyncio

        await _eval_js(self._session, "window.location.assign(" + json.dumps(url) + ")")
        await asyncio.sleep(1.5)


class _FetchResult:
    def __init__(self, resp: httpx.Response) -> None:
        self.status_code = resp.status_code
        self.headers = dict(resp.headers)
        self.text = resp.text

    def json(self) -> Any:
        return json.loads(self.text)


def register_fetch_tool(tools: Tools) -> None:
    """Register an HTTP fetch tool — the v3 cloud 'FETCH' capability.

    Allows the agent to call external APIs without going through the browser.
    """

    @tools.action(
        "Make a single HTTP request to an external JSON API or REST endpoint "
        "(server-side, no CORS). For fetching page HTML, embedded page data, or "
        "bulk/parallel fetches, prefer the code sandbox's fetch() helper instead."
    )
    async def http_fetch(
        url: str,
        method: str = "GET",
        headers: str | None = None,
        body: str | None = None,
    ) -> ActionResult:
        """Make an HTTP request.

        Args:
            url: The URL to request
            method: HTTP method (GET, POST, PUT, DELETE, PATCH)
            headers: JSON string of headers, e.g. '{"Authorization": "Bearer ..."}'
            body: Request body as string (for POST/PUT/PATCH)
        """
        parsed_headers: dict[str, str] = {}
        if headers:
            try:
                parsed_headers = json.loads(headers)
            except json.JSONDecodeError:
                return ActionResult(error="Invalid JSON in headers parameter")

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.request(
                    method=method.upper(),
                    url=url,
                    headers=parsed_headers,
                    content=body,
                )
                text = resp.text[:50_000]
                result = {
                    "status_code": resp.status_code,
                    "headers": dict(resp.headers),
                    "body": text,
                }
                return ActionResult(extracted_content=json.dumps(result, indent=2))
        except httpx.HTTPError as e:
            return ActionResult(error=f"HTTP request failed: {e}")


def _parse_capsolver_cost(result: dict[str, Any]) -> float:
    """Read the per-solve USD cost Capsolver returns in its task result."""
    try:
        return float(result.get("cost") or 0)
    except (ValueError, TypeError):
        return 0.0


def register_capsolver_tool(tools: Tools, cost_sink: list[float] | None = None) -> None:
    """Register the Capsolver CAPTCHA-solving tool on a Tools instance.

    Each solved CAPTCHA's real cost (from Capsolver's response) is appended to
    ``cost_sink`` if given, so the caller can fold it into the run's total cost.
    """

    if not settings.capsolver_api_key:
        logger.warning("CAPSOLVER_API_KEY not set — CAPTCHA tool disabled")
        return

    @tools.action(
        "Solve a CAPTCHA challenge on the current page. "
        "Call this when you encounter a Cloudflare challenge, reCAPTCHA, hCaptcha, or similar."
    )
    async def solve_captcha(
        captcha_type: str,
        browser_session: BrowserSession,
        site_key: str | None = None,
    ) -> ActionResult:
        """Attempt to solve a CAPTCHA using Capsolver.

        Args:
            captcha_type: One of 'recaptcha_v2', 'recaptcha_v3', 'hcaptcha', 'turnstile'
            browser_session: Injected by browser-use — must be named exactly this
            site_key: The site key from the page's CAPTCHA widget (if detectable)
        """
        current_url = ""
        try:
            current_url = await _eval_js(browser_session, "window.location.href") or ""
        except Exception:
            pass

        task_type_map: dict[str, str] = {
            "recaptcha_v2": "ReCaptchaV2TaskProxyLess",
            "recaptcha_v3": "ReCaptchaV3TaskProxyLess",
            "hcaptcha": "HCaptchaTaskProxyLess",
            "turnstile": "AntiTurnstileTaskProxyLess",
        }
        task_type = task_type_map.get(captcha_type)
        if not task_type:
            return ActionResult(
                error=f"Unknown captcha type: {captcha_type}. "
                f"Supported: {', '.join(task_type_map.keys())}"
            )

        if not site_key:
            try:
                site_key = await _eval_js(
                    browser_session,
                    """(function() {
                        var rc = document.querySelector('[data-sitekey]');
                        if (rc) return rc.getAttribute('data-sitekey');
                        var cf = document.querySelector('[data-cf-turnstile-sitekey]')
                            || document.querySelector('.cf-turnstile');
                        if (cf) return cf.getAttribute('data-sitekey')
                            || cf.getAttribute('data-cf-turnstile-sitekey');
                        return null;
                    })()""",
                )
            except Exception:
                pass

        if not site_key:
            return ActionResult(
                error="Could not detect CAPTCHA site key. "
                "Please provide the site_key parameter."
            )

        task_payload: dict[str, str | float] = {
            "type": task_type,
            "websiteURL": current_url,
            "websiteKey": site_key,
        }
        if captcha_type == "recaptcha_v3":
            task_payload["pageAction"] = "verify"
            task_payload["minScore"] = 0.9

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{CAPSOLVER_API}/createTask",
                    json={
                        "clientKey": settings.capsolver_api_key,
                        "task": task_payload,
                    },
                    timeout=30.0,
                )
                result = resp.json()
                if result.get("errorId", 0) != 0:
                    return ActionResult(
                        error=f"Capsolver error: {result.get('errorDescription', 'unknown')}"
                    )

                task_id = result.get("taskId")
                if not task_id:
                    solution = result.get("solution", {})
                    token = solution.get("gRecaptchaResponse") or solution.get("token")
                    if token:
                        if cost_sink is not None:
                            cost_sink.append(_parse_capsolver_cost(result))
                        await _inject_token(browser_session, captcha_type, token)
                        return ActionResult(
                            extracted_content="CAPTCHA solved successfully"
                        )
                    return ActionResult(error="No taskId or immediate solution returned")

                import asyncio

                for _ in range(60):
                    await asyncio.sleep(2)
                    resp = await client.post(
                        f"{CAPSOLVER_API}/getTaskResult",
                        json={
                            "clientKey": settings.capsolver_api_key,
                            "taskId": task_id,
                        },
                        timeout=30.0,
                    )
                    result = resp.json()
                    status = result.get("status")
                    if status == "ready":
                        if cost_sink is not None:
                            cost_sink.append(_parse_capsolver_cost(result))
                        solution = result.get("solution", {})
                        token = (
                            solution.get("gRecaptchaResponse")
                            or solution.get("token")
                            or solution.get("text")
                        )
                        if token:
                            await _inject_token(browser_session, captcha_type, token)
                            return ActionResult(
                                extracted_content="CAPTCHA solved successfully"
                            )
                        return ActionResult(error="Solution has no token")
                    elif status == "failed":
                        return ActionResult(
                            error=f"Capsolver failed: {result.get('errorDescription', 'unknown')}"
                        )

                return ActionResult(error="Capsolver timed out after 2 minutes")

        except httpx.HTTPError as e:
            return ActionResult(error=f"Capsolver HTTP error: {e}")


async def _inject_token(
    browser_session: BrowserSession, captcha_type: str, token: str
) -> None:
    """Inject the solved CAPTCHA token into the page via CDP."""
    token_json = json.dumps(token)
    if captcha_type in ("recaptcha_v2", "recaptcha_v3"):
        await _eval_js(
            browser_session,
            f"""(function() {{
                var token = {token_json};
                var el = document.getElementById('g-recaptcha-response');
                if (el) el.value = token;
                if (typeof ___grecaptcha_cfg !== 'undefined') {{
                    Object.entries(___grecaptcha_cfg.clients).forEach(function(entry) {{
                        var k = entry[0]; var v = entry[1];
                        var cb = (v && v.S && v.S.S && v.S.S.callback)
                            || (v && v.R && v.R.R && v.R.R.callback);
                        if (cb) cb(token);
                    }});
                }}
            }})()""",
        )
    elif captcha_type == "hcaptcha":
        await _eval_js(
            browser_session,
            f"""(function() {{
                var token = {token_json};
                var textarea = document.querySelector('[name="h-captcha-response"]');
                if (textarea) textarea.value = token;
            }})()""",
        )
    elif captcha_type == "turnstile":
        await _eval_js(
            browser_session,
            f"""(function() {{
                var token = {token_json};
                var input = document.querySelector('[name="cf-turnstile-response"]');
                if (input) input.value = token;
                if (window.turnstile) turnstile.getResponse = function() {{ return token; }};
            }})()""",
        )


def register_python_sandbox_tool(tools: Tools) -> None:
    """Register a stateful, browser-connected Python sandbox — the v3 cloud sandbox
    capability. Code runs in-process against the live page with a namespace that
    persists across calls, so the agent can read embedded page data
    (``window.__NEXT_DATA__``, ``__appData``, JSON-LD) and bulk-fetch detail pages
    the way the cloud does.

    @nonobvious(forced-by): in-process ``exec`` (not a subprocess) is required so the
    code can reach the live BrowserSession/CDP; acceptable on this single-tenant,
    owner-operated Pi.
    """
    namespace: dict[str, Any] = {}

    @tools.action(
        "Execute Python in a persistent, browser-connected sandbox. Variables persist "
        "across calls; use top-level await. Helpers: browser.evaluate(js), "
        "browser.get_html(selector=None), browser.navigate(url); fetch(url, method='GET', "
        "headers=None, body=None) returning an object with .status_code/.text/.json() "
        "for server-side HTTP (no CORS); save_json(obj, name). Plus asyncio, json, re and "
        "the standard library. Ideal for parsing embedded page JSON and bulk-fetching "
        "detail pages. Print results with print()."
    )
    async def run_python(code: str, browser_session: BrowserSession) -> ActionResult:
        import ast
        import asyncio
        import contextlib
        import io

        results: dict[str, Any] = namespace.setdefault("__results__", {})

        def _save_json(obj: Any, name: str = "output.json") -> str:
            results[name] = obj
            return name

        async def _fetch(
            url: str,
            method: str = "GET",
            headers: dict | None = None,
            body: str | None = None,
            output_format: str = "raw",
            timeout_ms: int = 30000,
        ) -> _FetchResult:
            async with httpx.AsyncClient(timeout=timeout_ms / 1000) as client:
                resp = await client.request(
                    method.upper(), url, headers=headers or {}, content=body
                )
            return _FetchResult(resp)

        namespace.update(
            {
                "browser": _SandboxBrowser(browser_session),
                "fetch": _fetch,
                "save_json": _save_json,
                "save_checkpoint_json": _save_json,
                "asyncio": asyncio,
                "json": json,
                "re": re,
            }
        )

        try:
            compiled = compile(
                code, "<sandbox>", "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT
            )
        except SyntaxError as e:
            return ActionResult(error=f"Syntax error: {e}")

        stdout = io.StringIO()

        async def _run() -> None:
            with contextlib.redirect_stdout(stdout):
                coro = eval(compiled, namespace)
                if coro is not None:
                    await coro

        try:
            await asyncio.wait_for(_run(), timeout=45.0)
        except asyncio.TimeoutError:
            return ActionResult(
                error="Python execution timed out after 45 seconds. Keep cells small; "
                "fetch with bounded concurrency and save progress."
            )
        except Exception as e:
            out = stdout.getvalue()
            tail = f"\n--- stdout ---\n{out}" if out else ""
            return ActionResult(error=f"{type(e).__name__}: {e}{tail}"[:10000])

        out = stdout.getvalue()[:50_000]
        if results:
            out = (out + "\n" + "\n".join(f"[saved {k}]" for k in results)).strip()
        return ActionResult(extracted_content=out or "(no output)")
