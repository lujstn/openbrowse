"""Custom browser-use tools — Capsolver CAPTCHA solving, Python sandbox, HTTP fetch."""

from __future__ import annotations

import json
import logging

import httpx
from browser_use import ActionResult, Controller
from browser_use.browser.context import BrowserContext

from app.config import settings

logger = logging.getLogger(__name__)

CAPSOLVER_API = "https://api.capsolver.com"


def register_fetch_tool(controller: Controller) -> None:
    """Register an HTTP fetch tool — the v3 cloud 'FETCH' capability.

    Allows the agent to call external APIs without going through the browser.
    """

    @controller.action(
        "Make an HTTP request to an external API. Use this for JSON APIs, "
        "REST endpoints, or any HTTP call that doesn't need a browser. "
        "Do NOT use this for loading web pages — use browser_navigate instead."
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


def register_capsolver_tool(controller: Controller) -> None:
    """Register the Capsolver CAPTCHA-solving tool on a Controller."""

    if not settings.capsolver_api_key:
        logger.warning("CAPSOLVER_API_KEY not set — CAPTCHA tool disabled")
        return

    @controller.action(
        "Solve a CAPTCHA challenge on the current page. "
        "Call this when you encounter a Cloudflare challenge, reCAPTCHA, hCaptcha, or similar."
    )
    async def solve_captcha(
        captcha_type: str,
        site_key: str | None = None,
        browser: BrowserContext | None = None,
    ) -> ActionResult:
        """Attempt to solve a CAPTCHA using Capsolver.

        Args:
            captcha_type: One of 'recaptcha_v2', 'recaptcha_v3', 'hcaptcha', 'turnstile'
            site_key: The site key from the page's CAPTCHA widget (if detectable)
            browser: Injected by browser-use
        """
        if not browser:
            return ActionResult(error="No browser context available")

        page = await browser.get_current_page()
        current_url = page.url

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
                site_key = await page.evaluate(
                    """() => {
                        const rc = document.querySelector('[data-sitekey]');
                        if (rc) return rc.getAttribute('data-sitekey');
                        const cf = document.querySelector('[data-cf-turnstile-sitekey]')
                            || document.querySelector('.cf-turnstile');
                        if (cf) return cf.getAttribute('data-sitekey')
                            || cf.getAttribute('data-cf-turnstile-sitekey');
                        return null;
                    }"""
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
                        await _inject_token(page, captcha_type, token)
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
                        solution = result.get("solution", {})
                        token = (
                            solution.get("gRecaptchaResponse")
                            or solution.get("token")
                            or solution.get("text")
                        )
                        if token:
                            await _inject_token(page, captcha_type, token)
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


async def _inject_token(page: object, captcha_type: str, token: str) -> None:
    """Inject the solved CAPTCHA token into the page."""
    if captcha_type in ("recaptcha_v2", "recaptcha_v3"):
        await page.evaluate(  # type: ignore[attr-defined]
            """(token) => {
                document.getElementById('g-recaptcha-response').value = token;
                if (typeof ___grecaptcha_cfg !== 'undefined') {
                    Object.entries(___grecaptcha_cfg.clients).forEach(([k, v]) => {
                        const cb = v?.S?.S?.callback || v?.R?.R?.callback;
                        if (cb) cb(token);
                    });
                }
            }""",
            token,
        )
    elif captcha_type == "hcaptcha":
        await page.evaluate(  # type: ignore[attr-defined]
            """(token) => {
                const textarea = document.querySelector('[name="h-captcha-response"]');
                if (textarea) textarea.value = token;
            }""",
            token,
        )
    elif captcha_type == "turnstile":
        await page.evaluate(  # type: ignore[attr-defined]
            """(token) => {
                const input = document.querySelector('[name="cf-turnstile-response"]');
                if (input) input.value = token;
                if (window.turnstile) turnstile.getResponse = () => token;
            }""",
            token,
        )


def register_python_sandbox_tool(controller: Controller) -> None:
    """Register a Python code execution sandbox — the v3 cloud sandbox capability."""

    @controller.action(
        "Execute Python code in a sandboxed environment. Use this for data processing, "
        "parsing HTML/JSON, calculations, string manipulation, or any task that's easier "
        "in Python than in the browser. The code runs in an isolated subprocess. "
        "You can use standard library modules. Print results to stdout."
    )
    async def run_python(
        code: str,
    ) -> ActionResult:
        """Execute Python code and return stdout/stderr.

        Args:
            code: Python code to execute. Use print() to output results.
        """
        import asyncio
        import tempfile
        from pathlib import Path

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False
        ) as f:
            f.write(code)
            script_path = f.name

        try:
            proc = await asyncio.create_subprocess_exec(
                "python3",
                script_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=30.0
                )
            except asyncio.TimeoutError:
                proc.kill()
                return ActionResult(
                    error="Python execution timed out after 30 seconds"
                )

            stdout_str = stdout.decode("utf-8", errors="replace")[:50_000]
            stderr_str = stderr.decode("utf-8", errors="replace")[:10_000]

            if proc.returncode != 0:
                return ActionResult(
                    error=f"Python exited with code {proc.returncode}\nstderr: {stderr_str}"
                )

            result = stdout_str
            if stderr_str:
                result += f"\n--- stderr ---\n{stderr_str}"

            return ActionResult(extracted_content=result)
        finally:
            Path(script_path).unlink(missing_ok=True)
