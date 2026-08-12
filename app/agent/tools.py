"""Custom browser-use tools — Capsolver CAPTCHA solving, Python sandbox, HTTP fetch."""

import asyncio
import json
import logging
import re
from typing import Any
from urllib.parse import urljoin

import httpx
from browser_use import ActionResult, BrowserSession, Tools
from browser_use.browser.events import CloseTabEvent, NavigateToUrlEvent, SwitchTabEvent
from browser_use.filesystem.file_system import FileSystem

from app.agent.output_store import OutputStore
from app.config import settings

logger = logging.getLogger(__name__)

CAPSOLVER_API = "https://api.capsolver.com"

_MAX_INLINE_FETCH_CHARS = 3000
_FETCH_PREVIEW_CHARS = 2000
_SANDBOX_STDOUT_PREVIEW_CHARS = 2500
_CAPPED_READ_PREVIEW_CHARS = 8000
_FS_EXTENSIONS = {"md", "txt", "json", "jsonl", "csv", "pdf", "docx", "html", "xml"}


def _normalise_fs_name(name: str, default_ext: str = "json") -> str:
    """Coerce a caller-supplied name into a FileSystem-valid filename with a
    supported extension so ``write_file`` accepts it.
    """
    base = ((name or "").strip() or f"output.{default_ext}").rsplit("/", 1)[-1]
    if "." in base and base.rsplit(".", 1)[1].lower() in _FS_EXTENSIONS:
        return base
    return f"{base}.{default_ext}"


def _fs_name_from_url(url: str, content_type: str = "", body: str = "") -> str:
    """A stable, readable filename derived from a URL (same URL -> same file, so a
    re-fetch overwrites rather than duplicates), with an extension inferred from
    the content type or body.
    """
    from urllib.parse import urlparse

    parsed = urlparse(url)
    base = re.sub(r"[^a-zA-Z0-9_\-]", "_", f"{parsed.netloc}{parsed.path}".strip("/"))[:60]
    base = base.strip("_") or "response"
    ct = (content_type or "").lower()
    head = (body or "").lstrip()[:16].lower()
    if "json" in ct or head[:1] in ("{", "["):
        ext = "json"
    elif "html" in ct or head.startswith(("<!doctype html", "<html")):
        ext = "html"
    elif "xml" in ct or head.startswith("<?xml"):
        ext = "xml"
    else:
        ext = "txt"
    return f"fetch_{base}.{ext}"


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
        "Make a single server-side HTTP request to an external JSON API or REST "
        "endpoint (no CORS). Large responses (>3000 chars) are saved to a file and "
        "only previewed here — read specific keys/slices via read_file or the "
        "run_python sandbox (read_json) instead of re-fetching. For fetching page "
        "HTML, embedded page data, or bulk/parallel fetches, prefer the code "
        "sandbox's fetch() helper instead."
    )
    async def http_fetch(
        url: str,
        file_system: FileSystem,
        method: str = "GET",
        headers: str | None = None,
        body: str | None = None,
    ) -> ActionResult:
        """Make an HTTP request.

        Args:
            url: The URL to request
            file_system: Injected by browser-use — must be named exactly this
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
            text = resp.text
            total = len(text)
            if total <= _MAX_INLINE_FETCH_CHARS:
                result = {
                    "status_code": resp.status_code,
                    "headers": dict(resp.headers),
                    "body": text,
                }
                return ActionResult(extracted_content=json.dumps(result, indent=2))

            saved: str | None = _fs_name_from_url(
                url, resp.headers.get("content-type", ""), text
            )
            try:
                await file_system.write_file(saved, text)
            except Exception:
                logger.warning("http_fetch: failed to save large body to FileSystem", exc_info=True)
                saved = None
            result = {
                "status_code": resp.status_code,
                "headers": dict(resp.headers),
                "body_preview": text[:_FETCH_PREVIEW_CHARS],
                "total_chars": total,
                "saved_file": saved,
                "note": (
                    f"Body is {total} chars; only the first {_FETCH_PREVIEW_CHARS} are shown. "
                    + (
                        f"Full body saved to '{saved}' — read specific parts via read_file "
                        "or the run_python sandbox (read_json) rather than re-fetching."
                        if saved
                        else "Saving the full body failed; narrow the request instead of re-fetching."
                    )
                ),
            }
            return ActionResult(
                extracted_content=json.dumps(result, indent=2),
                long_term_memory=f"Fetched {url} ({total} chars) -> {saved or 'preview only'}",
            )
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


def register_python_sandbox_tool(
    tools: Tools, clipboard: dict[str, Any] | None = None
) -> None:
    """Register a stateful, browser-connected Python sandbox — the v3 cloud sandbox
    capability. Code runs in-process against the live page with a namespace that
    persists across calls, so the agent can read embedded page data
    (``window.__NEXT_DATA__``, ``__appData``, JSON-LD) and bulk-fetch detail pages
    the way the cloud does. Large data is kept out of the model context: it lives in
    sandbox variables (which persist) or in FileSystem files via ``save_json``.

    @nonobvious(forced-by): in-process ``exec`` (not a subprocess) is required so the
    code can reach the live BrowserSession/CDP; acceptable on this single-tenant,
    owner-operated Pi.
    """
    namespace: dict[str, Any] = {}
    if clipboard is None:
        clipboard = {}

    @tools.action(
        "Execute Python in a persistent, browser-connected sandbox. Variables persist "
        "across calls (assign large results to a variable instead of re-fetching); use "
        "top-level await. Helpers: browser.evaluate(js), browser.get_html(selector=None), "
        "browser.navigate(url); fetch(url, method='GET', headers=None, body=None) returning "
        "an object with .status_code/.text/.json() for server-side HTTP (no CORS); "
        "await save_json(obj, name) / await read_json(name) to persist/read JSON as "
        "FileSystem files the native read_file also sees; remember(key, value)/recall(key) "
        "for the shared clipboard. Plus asyncio, json, re and the standard library. STDOUT "
        "is truncated to a small preview — never print whole blobs; save them and print "
        "only specific keys/slices."
    )
    async def run_python(
        code: str, browser_session: BrowserSession, file_system: FileSystem
    ) -> ActionResult:
        import ast
        import contextlib
        import io

        async def _save_json(obj: Any, name: str = "output.json") -> str:
            fname = _normalise_fs_name(name, "json")
            await file_system.write_file(fname, json.dumps(obj, indent=2, default=str))
            return fname

        async def _read_json(name: str) -> Any:
            fname = _normalise_fs_name(name, "json")
            file_obj = file_system.get_file(fname) or file_system.get_file(name)
            if file_obj is None:
                raise FileNotFoundError(f"No saved file named {name!r}")
            return json.loads(file_obj.read())

        def _remember(key: str, value: Any) -> str:
            clipboard[str(key)] = value
            return str(key)

        def _recall(key: str, default: Any = None) -> Any:
            return clipboard.get(str(key), default)

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
                "read_json": _read_json,
                "remember": _remember,
                "recall": _recall,
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

        out = stdout.getvalue()
        total = len(out)
        preview = out[:_SANDBOX_STDOUT_PREVIEW_CHARS]
        if total > _SANDBOX_STDOUT_PREVIEW_CHARS:
            preview += (
                f"\n\n[stdout truncated: {total} chars total. Assign large results to a "
                "variable (it persists across cells) or save_json(obj,'name.json') then "
                "print only specific keys/slices; never print whole blobs.]"
            )
        return ActionResult(extracted_content=preview or "(no output)")


def register_clipboard_tools(tools: Tools, clipboard: dict[str, Any]) -> None:
    """Register a per-session key/value clipboard (shared with the sandbox's
    ``remember``/``recall``) so the agent can stash URLs, IDs and counts and
    return to them after detours.
    """

    @tools.action(
        "Save a value to the session clipboard under a key so you can return to it "
        "later (e.g. a listings URL, an id, a running count). Persists across steps "
        "and is shared with the run_python sandbox (remember/recall)."
    )
    async def remember(key: str, value: str) -> ActionResult:
        clipboard[str(key)] = value
        return ActionResult(
            extracted_content=f"Remembered {key}", long_term_memory=f"remember({key})"
        )

    @tools.action(
        "Fetch a value previously saved with remember (or an auto-populated key such "
        "as startUrl) from the session clipboard."
    )
    async def recall(key: str) -> ActionResult:
        if str(key) not in clipboard:
            known = ", ".join(sorted(clipboard)) or "(empty)"
            return ActionResult(
                extracted_content=f"No value stored for '{key}'. Known keys: {known}"
            )
        value = clipboard[str(key)]
        return ActionResult(
            extracted_content=str(value),
            long_term_memory=f"recall({key})={str(value)[:100]}",
        )


class TabManager:
    """Per-session lazy multi-tab manager. A stable ordered queue maps index ``n``
    (the nth queued URL) to a browser-use tab. URLs are queued as lightweight
    about:blank tabs and only loaded on demand; at most the base/start tab plus the
    two most recently loaded tabs stay live, older loaded tabs revert to about:blank
    to free memory while keeping their queue slot so ``goto_tab(n)`` can reopen them.
    """

    MAX_QUEUED = 48
    MAX_LOADED = 2

    def __init__(self, session: BrowserSession) -> None:
        self._session = session
        self._urls: list[str] = []
        self._target_ids: list[str | None] = []
        self._loaded: list[int] = []
        self._base_target_id: str | None = None

    async def _open_blank(self) -> str | None:
        before = {t.target_id for t in await self._session.get_tabs()}
        event = self._session.event_bus.dispatch(
            NavigateToUrlEvent(url="about:blank", new_tab=True)
        )
        await event
        await event.event_result(raise_if_any=False, raise_if_none=False)
        after = await self._session.get_tabs()
        new = [t for t in after if t.target_id not in before]
        return new[0].target_id if new else None

    async def _switch(self, target_id: str | None) -> None:
        if not target_id:
            return
        event = self._session.event_bus.dispatch(SwitchTabEvent(target_id=target_id))
        await event
        await event.event_result(raise_if_any=False, raise_if_none=False)

    async def _navigate_current(self, url: str) -> None:
        event = self._session.event_bus.dispatch(
            NavigateToUrlEvent(url=url, new_tab=False)
        )
        await event
        await event.event_result(raise_if_any=False, raise_if_none=False)

    async def _is_valid(self, target_id: str | None) -> bool:
        if not target_id:
            return False
        try:
            return await self._session.session_manager.is_target_valid(target_id)
        except Exception:
            return False

    async def _track_loaded(self, n: int) -> list[int]:
        """Mark queue index ``n`` as loaded and revert older loaded tabs beyond
        ``MAX_LOADED`` to about:blank, keeping live tabs bounded to base +
        MAX_LOADED. Returns the indices that were reverted.
        """
        if n in self._loaded:
            self._loaded.remove(n)
        self._loaded.append(n)

        reverted: list[int] = []
        while len(self._loaded) > self.MAX_LOADED:
            old = self._loaded.pop(0)
            if old == n:
                self._loaded.append(old)
                break
            old_target = self._target_ids[old]
            if await self._is_valid(old_target):
                try:
                    await self._switch(old_target)
                    await self._navigate_current("about:blank")
                    reverted.append(old)
                except Exception:
                    logger.debug("_track_loaded: failed to revert tab %s", old, exc_info=True)
        return reverted

    async def open_tabs(self, urls: list[str]) -> str:
        if self._base_target_id is None:
            self._base_target_id = self._session.agent_focus_target_id
        remaining = self.MAX_QUEUED - len(self._urls)
        if remaining <= 0:
            return (
                f"Queue is full ({self.MAX_QUEUED} tabs). goto_tab(n) still works on "
                "the existing slots."
            )
        start = len(self._urls)
        queued = 0
        for url in urls[:remaining]:
            self._urls.append(url)
            self._target_ids.append(await self._open_blank())
            queued += 1
        await self._switch(self._base_target_id)
        return (
            f"Queued {queued} URL(s) as blank/unloaded tabs at indices "
            f"{start}..{len(self._urls) - 1} (0-based). They are NOT loaded; call "
            f"goto_tab(n) to load one on demand. Your start/base tab is unaffected."
        )

    async def goto_tab(self, n: int) -> str:
        if not self._urls:
            return "No tabs queued yet; call open_tabs([...]) first."
        if n < 0 or n >= len(self._urls):
            return f"No tab at index {n}. Valid range: 0..{len(self._urls) - 1}."

        url = self._urls[n]
        if not await self._is_valid(self._target_ids[n]):
            self._target_ids[n] = await self._open_blank()
            await self._switch(self._base_target_id)
        target_id = self._target_ids[n]
        if not target_id:
            return f"Could not open a live tab for index {n}."

        await self._switch(target_id)
        await self._navigate_current(url)
        await asyncio.sleep(1.0)

        reverted = await self._track_loaded(n)

        await self._switch(target_id)
        note = f"Loaded index {n} ({url}) and switched to it."
        if reverted:
            note += (
                f" Reverted older loaded tab(s) {reverted} to about:blank to free "
                "memory; goto_tab reopens them."
            )
        return note

    async def open_in_new_tab(self, index: int) -> str:
        node = await self._session.get_element_by_index(index)
        if node is None:
            return f"No element at index {index}."

        href = (node.attributes or {}).get("href")
        if not href:
            return f"Element {index} has no href attribute."

        current = await _eval_js(self._session, "window.location.href")
        abs_url = urljoin(current or "", href)

        if self._base_target_id is None:
            self._base_target_id = self._session.agent_focus_target_id

        before = {t.target_id for t in await self._session.get_tabs()}
        event = self._session.event_bus.dispatch(
            NavigateToUrlEvent(url=abs_url, new_tab=True)
        )
        await event
        await event.event_result(raise_if_any=False, raise_if_none=False)
        after = await self._session.get_tabs()
        new = [t for t in after if t.target_id not in before]
        target_id = new[0].target_id if new else None
        if not target_id:
            return f"Could not open a new tab for {abs_url}."

        n = len(self._urls)
        self._urls.append(abs_url)
        self._target_ids.append(target_id)
        reverted = await self._track_loaded(n)

        await self._switch(target_id)
        note = f"Opened {abs_url} in a new tab (index {n}) and switched to it."
        if reverted:
            note += (
                f" Reverted older loaded tab(s) {reverted} to about:blank to free "
                "memory; goto_tab reopens them."
            )
        return note

    async def close_tab(self) -> str:
        current = self._session.agent_focus_target_id
        if self._base_target_id is None or current is None or current == self._base_target_id:
            return "Already on the base tab; nothing to close."

        await self._switch(self._base_target_id)
        event = self._session.event_bus.dispatch(CloseTabEvent(target_id=current))
        await event
        await event.event_result(raise_if_any=False, raise_if_none=False)

        if current in self._target_ids:
            n = self._target_ids.index(current)
            self._target_ids[n] = None
            if n in self._loaded:
                self._loaded.remove(n)

        return "Closed the tab and returned to the base tab."


def register_tab_tools(tools: Tools, tab_manager: TabManager) -> None:
    """Register the lazy multi-tab fan-out actions on a Tools instance."""

    @tools.action(
        "Queue many URLs as lightweight, UNLOADED background tabs for efficient "
        "fan-out (hard cap 48 total). Each becomes a blank about:blank tab at a stable "
        "0-based index; the real URL is only fetched when you call goto_tab(n). Your "
        "current/start tab is left untouched."
    )
    async def open_tabs(urls: list[str]) -> ActionResult:
        try:
            note = await tab_manager.open_tabs(urls)
            return ActionResult(extracted_content=note, long_term_memory=note)
        except Exception as e:
            return ActionResult(error=f"open_tabs failed: {type(e).__name__}: {e}")

    @tools.action(
        "Load and switch to queued tab index n (0-based, from open_tabs): navigate it "
        "from about:blank to its URL and focus it. Memory-bounded — only the base tab "
        "plus the two most recently loaded tabs stay live; older loaded tabs revert to "
        "about:blank but keep their index so goto_tab(n) reopens them. n always means "
        "the nth queued URL regardless of live state."
    )
    async def goto_tab(n: int) -> ActionResult:
        try:
            note = await tab_manager.goto_tab(n)
            return ActionResult(extracted_content=note, long_term_memory=note)
        except Exception as e:
            return ActionResult(error=f"goto_tab failed: {type(e).__name__}: {e}")

    @tools.action(
        "Open the link at element index N in a new tab and switch to it — works "
        "even for links inside embedded/cross-origin sections that find_elements "
        "can't read. Use this to visit a listing's detail page."
    )
    async def open_in_new_tab(index: int) -> ActionResult:
        try:
            note = await tab_manager.open_in_new_tab(index)
            return ActionResult(extracted_content=note, long_term_memory=note)
        except Exception as e:
            return ActionResult(error=f"open_in_new_tab failed: {type(e).__name__}: {e}")

    @tools.action(
        "Close the current tab and return to your base/start tab — use after "
        "you've extracted what you need, before moving to the next."
    )
    async def close_tab() -> ActionResult:
        try:
            note = await tab_manager.close_tab()
            return ActionResult(extracted_content=note, long_term_memory=note)
        except Exception as e:
            return ActionResult(error=f"close_tab failed: {type(e).__name__}: {e}")

    @tools.action(
        "List every link (index, text, href) on the current page, INCLUDING those "
        "inside embedded/cross-origin panels that find_elements cannot reach. Returns "
        "the full list inline — the hrefs are the payload, so trust them and follow "
        "one with open_in_new_tab(index) rather than hunting for another route."
    )
    async def list_links(browser_session: BrowserSession) -> ActionResult:
        try:
            selector_map = await browser_session.get_selector_map()
            current = await _eval_js(browser_session, "window.location.href") or ""

            links: list[dict[str, Any]] = []
            for index in sorted(selector_map):
                node = selector_map[index]
                if node.tag_name != "a":
                    continue
                href = (node.attributes or {}).get("href")
                if not href:
                    continue
                links.append(
                    {
                        "index": index,
                        "text": node.get_meaningful_text_for_llm()[:150],
                        "href": urljoin(current, href),
                    }
                )
        except Exception as e:
            return ActionResult(error=f"list_links failed: {type(e).__name__}: {e}")

        return ActionResult(
            extracted_content=json.dumps(links, indent=2),
            long_term_memory=f"Listed {len(links)} link(s) on the page.",
        )


def register_capped_read_overrides(tools: Tools) -> None:
    """Overwrite the built-in ``find_elements``/``evaluate`` actions with wrappers
    that cap their output so a full-page dump can't blow the agent's context.

    Re-registers under the same name rather than using ``tools.exclude_action``
    (which blocks re-registration outright) — the registry keys actions by
    ``func.__name__`` (tools/registry/service.py), so registering a function of
    the same name simply replaces the dict entry, last-wins.
    """
    registry_actions = tools.registry.registry.actions

    async def _cap_readout(
        result: ActionResult, file_system: FileSystem, readout_name: str
    ) -> ActionResult:
        content = result.extracted_content
        if content and len(content) > _CAPPED_READ_PREVIEW_CHARS:
            total = len(content)
            try:
                await file_system.write_file(readout_name, content)
                tail = f"saved to '{readout_name}' — read specific parts instead of dumping"
            except Exception:
                logger.warning(
                    "register_capped_read_overrides: failed to save readout to '%s'",
                    readout_name,
                    exc_info=True,
                )
                tail = "could not be saved; narrow your query instead of dumping"
            result.extracted_content = (
                content[:_CAPPED_READ_PREVIEW_CHARS]
                + f"\n[truncated: {total} chars total, {tail}]"
            )
        return result

    find_elements_entry = registry_actions.get("find_elements")
    if find_elements_entry is not None:
        original_find_elements = find_elements_entry.function

        @tools.action(
            find_elements_entry.description,
            param_model=find_elements_entry.param_model,
            domains=find_elements_entry.domains,
            terminates_sequence=find_elements_entry.terminates_sequence,
        )
        async def find_elements(
            params: Any,
            browser_session: BrowserSession,
            file_system: FileSystem,
            _original: Any = original_find_elements,
        ) -> ActionResult:
            result = await _original(params=params, browser_session=browser_session)
            return await _cap_readout(result, file_system, "readout_find_elements.txt")

    evaluate_entry = registry_actions.get("evaluate")
    if evaluate_entry is not None:
        original_evaluate = evaluate_entry.function

        @tools.action(
            evaluate_entry.description,
            param_model=evaluate_entry.param_model,
            domains=evaluate_entry.domains,
            terminates_sequence=evaluate_entry.terminates_sequence,
        )
        async def evaluate(
            params: Any,
            browser_session: BrowserSession,
            file_system: FileSystem,
            _original: Any = original_evaluate,
        ) -> ActionResult:
            result = await _original(params=params, browser_session=browser_session)
            return await _cap_readout(result, file_system, "readout_evaluate.txt")


def _describe_item_fields(store: OutputStore) -> str:
    model = store.item_model
    if model is None:
        return "Each item is a free-form object."
    parts = [
        name if field.is_required() else f"{name}?"
        for name, field in model.model_fields.items()
    ]
    return "Each item has fields: " + ", ".join(parts) + " (? = optional)."


def _describe_top_fields(store: OutputStore) -> str:
    names = [n for n in store.output_model.model_fields if n != store.array_field]
    if not names:
        return "This output has no top-level scalar fields."
    return "Settable fields: " + ", ".join(names) + "."


async def _mirror_output(store: OutputStore, file_system: FileSystem) -> None:
    try:
        await file_system.write_file("output.json", store.read_output())
    except Exception:
        logger.warning("output store: failed to mirror output.json", exc_info=True)


def register_output_store_tools(tools: Tools, store: OutputStore) -> None:
    """Expose the schema-validated output store as agent actions. The store is the
    single answer surface: the agent fills it as it discovers data, every write is
    validated live and mirrored to ``output.json``, and the final result is read
    back from it after the run.
    """
    array = store.array_field or "output"

    @tools.action(
        f"Append one item to the '{array}' list — the primary answer array. "
        f"{_describe_item_fields(store)} Provide every field you already know now; "
        "enrich the rest with update_item once you have opened the item's own page. "
        "Validated against the schema and rejected if it does not fit."
    )
    async def add_item(item: dict[str, Any], file_system: FileSystem) -> ActionResult:
        ok, msg = store.add_item(item)
        if not ok:
            return ActionResult(error=msg)
        await _mirror_output(store, file_system)
        return ActionResult(extracted_content=msg, long_term_memory=msg)

    @tools.action(
        f"Enrich the item at integer index (0-based, as reported by add_item) in the "
        f"'{array}' list by merging in the given fields — this is how a detail-page "
        "visit fills a stub's missing values such as description or postedAt. "
        "Re-validated against the schema."
    )
    async def update_item(
        index: int, fields: dict[str, Any], file_system: FileSystem
    ) -> ActionResult:
        ok, msg = store.update_item(index, fields)
        if not ok:
            return ActionResult(error=msg)
        await _mirror_output(store, file_system)
        return ActionResult(extracted_content=msg, long_term_memory=msg)

    @tools.action(
        "Set a top-level, non-list output field, validated against its type. "
        + _describe_top_fields(store)
    )
    async def set_field(key: str, value: Any, file_system: FileSystem) -> ActionResult:
        ok, msg = store.set_field(key, value)
        if not ok:
            return ActionResult(error=msg)
        await _mirror_output(store, file_system)
        return ActionResult(extracted_content=msg, long_term_memory=msg)

    @tools.action(
        "Read the output you are building so far — the schema with everything you "
        "have filled in. Any empty or null field is unfinished work."
    )
    async def read_output() -> ActionResult:
        return ActionResult(extracted_content=store.read_output())

    @tools.action(
        "Search the output you have built so far for a case-insensitive substring "
        "across items and fields — use it to check whether you already recorded "
        "something before adding it again."
    )
    async def search_output(query: str) -> ActionResult:
        return ActionResult(extracted_content=store.search_output(query))


def register_completeness_gate(tools: Tools, store: OutputStore, on_incomplete) -> None:
    """One-shot soft gate on ``done``: the first time the agent tries to finish with
    schema fields still empty, bounce it back to fill them; accept the next done
    unconditionally so it can never loop. Re-registers ``done`` under the same name
    (last-wins in the registry) — the same override pattern as the capped-read
    wrappers. ``on_incomplete(empty_fields)`` is awaited once when the bounce fires.
    """
    registry_actions = tools.registry.registry.actions
    done_entry = registry_actions.get("done")
    if done_entry is None:
        return
    original_done = done_entry.function
    state = {"bounced": False}

    @tools.action(
        done_entry.description,
        param_model=done_entry.param_model,
        domains=done_entry.domains,
        terminates_sequence=done_entry.terminates_sequence,
    )
    async def done(params: Any, file_system: FileSystem) -> ActionResult:
        if not state["bounced"]:
            empties = store.empty_fields()
            if empties:
                state["bounced"] = True
                if on_incomplete is not None:
                    try:
                        await on_incomplete(empties)
                    except Exception:
                        logger.debug("completeness gate event emit failed", exc_info=True)
                listing = "\n- ".join(empties)
                return ActionResult(
                    is_done=False,
                    extracted_content=(
                        "Not finished — these fields in the output are still empty:\n- "
                        f"{listing}\n\nGo back to the page that could fill each one and "
                        "record it with add_item / update_item / set_field. A blank is "
                        "only acceptable once you have looked where the information "
                        "should be and found it genuinely absent. Then call done again."
                    ),
                )
        return await original_done(params=params, file_system=file_system)
