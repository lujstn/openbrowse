"""Custom browser-use tools — Capsolver CAPTCHA solving, Python sandbox, HTTP fetch."""

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx
from browser_use import ActionResult, BrowserSession, Tools
from browser_use.browser.events import (
    CloseTabEvent,
    NavigateToUrlEvent,
    SwitchTabEvent,
    TabCreatedEvent,
)
from browser_use.filesystem.file_system import FileSystem

from app.agent.output_store import OutputStore
from app.agent.textguard import guard_key
from app.config import settings

logger = logging.getLogger(__name__)

CAPSOLVER_API = "https://api.capsolver.com"

_MAX_INLINE_FETCH_CHARS = 3000
_FETCH_PREVIEW_CHARS = 2000
_SANDBOX_STDOUT_PREVIEW_CHARS = 2500
_CAPPED_READ_PREVIEW_CHARS = 8000
_GUARD_MIN_CHARS = 500
_FS_EXTENSIONS = {"md", "txt", "json", "jsonl", "csv", "pdf", "docx", "html", "xml"}


def _normalise_fs_name(name: str, default_ext: str = "json") -> str:
    """Coerce a caller-supplied name into a FileSystem-valid filename with a
    supported extension so ``write_file`` accepts it.
    """
    base = ((name or "").strip() or f"output.{default_ext}").rsplit("/", 1)[-1]
    if "." in base and base.rsplit(".", 1)[1].lower() in _FS_EXTENSIONS:
        return base
    return f"{base}.{default_ext}"


def _normalise_py_name(name: str) -> str:
    """Coerce a caller-supplied script name into a safe ``.py`` filename for the
    scripts scratch dir (which is outside FileSystem's own extension whitelist).
    """
    base = ((name or "").strip() or "script").rsplit("/", 1)[-1]
    base = re.sub(r"[^A-Za-z0-9_.-]", "_", base)
    if base.lower().endswith(".py"):
        return base
    base = base[:-1] if base.endswith(".") else base
    return f"{base or 'script'}.py"


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


def _norm_url(url: str) -> str:
    """Stable key for the visited-set: lowercase, drop the fragment and trailing slash,
    so ``…?ashby_jid=X#openings`` and the same URL without the fragment compare equal.
    """
    u = (url or "").strip().lower().split("#", 1)[0]
    return u.rstrip("/")


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
    ``browser`` handle): JavaScript eval and DOM access against the live page, plus
    per-target reads of cross-origin iframes that main-frame JS cannot reach.
    """

    def __init__(
        self, session: BrowserSession, clipboard: dict[str, Any] | None = None
    ) -> None:
        self._session = session
        self._clipboard = clipboard

    def _mark_visited(self, url: str) -> None:
        if self._clipboard is None or not url:
            return
        self._clipboard.setdefault("_visited", set()).add(_norm_url(url))

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

    async def frames(self) -> list[dict[str, str]]:
        """Every cross-origin iframe target on the page — the embedded panels that
        main-frame evaluate()/get_html() cannot see. Returns [{targetId, url}].

        @nonobvious(forced-by): same-origin policy blocks main-frame Runtime.evaluate
        from reading a cross-origin iframe; only a per-target CDP session reaches it,
        and only raw ``Target.getTargets`` (not get_all_frames/session_manager) lists
        OOPIF targets in browser-use 0.13.7.
        """
        cdp = await self._session.get_or_create_cdp_session()
        targets = await cdp.cdp_client.send.Target.getTargets()
        return [
            {"targetId": t["targetId"], "url": t.get("url", "")}
            for t in targets.get("targetInfos", [])
            if t.get("type") == "iframe"
        ]

    async def frame_evaluate(
        self, url_contains: str, js: str, all_matches: bool = False
    ) -> Any:
        """Run JS INSIDE a cross-origin iframe whose URL contains ``url_contains``, via
        a per-target CDP session. Returns the value of the first matching frame, or
        ``[(frame_url, value), …]`` when all_matches=True. This is the only way a script
        can read an embedded/cross-origin panel (e.g. a job description in an Ashby embed).
        """
        needle = (url_contains or "").lower()
        all_frames = await self.frames()
        matched = [f for f in all_frames if not needle or needle in f["url"].lower()]
        if not matched and all_frames:
            matched = all_frames
        results: list[tuple[str, Any]] = []
        for f in matched:
            try:
                sess = await self._session.get_or_create_cdp_session(
                    f["targetId"], focus=False
                )
                res = await sess.cdp_client.send.Runtime.evaluate(
                    params={"expression": js, "returnByValue": True, "awaitPromise": True},
                    session_id=sess.session_id,
                )
                if res.get("exceptionDetails"):
                    continue
                results.append((f["url"], res.get("result", {}).get("value")))
            except Exception:
                logger.debug("frame_evaluate failed on a frame", exc_info=True)
                continue
        if all_matches:
            return results
        return results[0][1] if results else None

    async def frame_text(self, url_contains: str) -> str:
        """The visible text of the matching cross-origin iframe (document.body.innerText)."""
        val = await self.frame_evaluate(
            url_contains, "document.body ? document.body.innerText : ''"
        )
        return val or ""

    async def wait_for_frame(self, url_contains: str, timeout_s: float = 12.0) -> bool:
        """Poll until a cross-origin iframe matching ``url_contains`` has rendered text,
        because an embed loads asynchronously after navigation. Returns True on success.
        """
        for _ in range(int(max(1.0, timeout_s) * 2)):
            txt = await self.frame_text(url_contains)
            if txt and txt.strip():
                return True
            await asyncio.sleep(0.5)
        return False

    async def navigate(
        self, url: str, wait_for: str | None = None, settle_s: float = 2.0
    ) -> None:
        """Navigate the current tab to ``url``. If ``wait_for`` is given, wait for a
        cross-origin iframe whose URL contains it to render; otherwise settle briefly.
        """
        await _eval_js(self._session, "window.location.assign(" + json.dumps(url) + ")")
        self._mark_visited(url)
        if wait_for:
            await self.wait_for_frame(wait_for, timeout_s=max(settle_s, 12.0))
        else:
            await asyncio.sleep(settle_s)


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
        "code sandbox (read_json) instead of re-fetching. For fetching page "
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
                        "or the code sandbox (read_json) rather than re-fetching."
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


async def _exec_in_sandbox(code: str, namespace: dict[str, Any]) -> ActionResult:
    """Compile and run one script against the persistent sandbox namespace, capturing
    stdout to a small preview. Shared by ``run_code_file`` — the only executor.
    """
    import ast
    import contextlib
    import io

    try:
        compiled = compile(code, "<script>", "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
    except SyntaxError as e:
        return ActionResult(error=f"Syntax error: {e}")

    stdout = io.StringIO()

    async def _run() -> None:
        with contextlib.redirect_stdout(stdout):
            coro = eval(compiled, namespace)
            if coro is not None:
                await coro

    try:
        await asyncio.wait_for(_run(), timeout=180.0)
    except asyncio.TimeoutError:
        return ActionResult(
            error="Script timed out after 180 seconds. Process a smaller batch of URLs "
            "per run, save progress with save_json, and continue in the next run."
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
            "variable (it persists across runs) or save_json(obj,'name.json') then "
            "print only specific keys/slices; never print whole blobs.]"
        )
    return ActionResult(extracted_content=preview or "(no output)")


def register_code_tools(tools: Tools, clipboard: dict[str, Any] | None = None) -> None:
    """Register the browser-connected code sandbox as a write-then-run pair — the v3
    cloud sandbox capability, but code can never execute directly. ``write_code_file``
    only persists a reusable script; ``run_code_file`` loads a saved script and runs
    it in-process against the live page, with a namespace that persists across runs so
    variables and imports carry over.

    @nonobvious(forced-by): in-process ``exec`` (not a subprocess) is required so the
    code can reach the live BrowserSession/CDP; acceptable on this single-tenant,
    owner-operated Pi.
    """
    namespace: dict[str, Any] = {}
    if clipboard is None:
        clipboard = {}

    def _scripts_dir(file_system: FileSystem) -> Path:
        d = file_system.get_dir().parent / "scripts"
        d.mkdir(parents=True, exist_ok=True)
        return d

    @tools.action(
        "Write a reusable Python script to a file (code is NEVER run here — save it, "
        "then execute with run_code_file). Write it to work on EVERY similar/templated "
        "page: read the page or its embedded panel and extract into a structure; "
        "parameterise anything page-specific. The FAST pattern for a listing: one script "
        "that loops the saved detail links, and for each — await browser.navigate(url, "
        "wait_for='<embed url part>'); text = await browser.frame_text('<embed url part>') "
        "— collects the fields, then save_json(rows, 'jobs.json'); finally add_items_from_file"
        "('jobs.json'). A field missing from the visible text (e.g. a posted/published date) "
        "is usually in the page's JSON-LD: frame_evaluate the script[type=application/ld+json] "
        "and parse its datePosted. Inside a script you have: browser.evaluate(js) / browser.get_html("
        "selector=None) for the MAIN page; browser.frames() / await browser.frame_text("
        "url_contains) / await browser.frame_evaluate(url_contains, js, all_matches=False) / "
        "await browser.wait_for_frame(url_contains) to READ INSIDE a cross-origin embed "
        "(the only way — main-frame evaluate/get_html cannot); await browser.navigate(url, "
        "wait_for=None); fetch(url,...) -> .status_code/.text/.json() (server-side, no CORS, "
        "and never a site's backend API); await save_json(obj, name) / await read_json(name); "
        "remember(key, value) / recall(key); plus asyncio, json, re. Variables persist across "
        "run_code_file calls."
    )
    async def write_code_file(
        name: str, code: str, file_system: FileSystem
    ) -> ActionResult:
        fname = _normalise_py_name(name)
        try:
            (_scripts_dir(file_system) / fname).write_text(code)
        except Exception as e:
            return ActionResult(error=f"write_code_file failed: {type(e).__name__}: {e}")
        note = (
            f"Saved script '{fname}'. Run it with run_code_file('{fname}', url=<page>). "
            "Reuse this same script on every similar page rather than writing new code."
        )
        return ActionResult(extracted_content=note, long_term_memory=f"wrote script {fname}")

    @tools.action(
        "Run a script previously saved with write_code_file, optionally navigating to "
        "url first, then executing it against the current page. Reuse the SAME script "
        "across every similar/templated page. STDOUT is truncated to a small preview — "
        "save large results with save_json and print only specific keys/slices."
    )
    async def run_code_file(
        name: str,
        browser_session: BrowserSession,
        file_system: FileSystem,
        url: str | None = None,
    ) -> ActionResult:
        fname = _normalise_py_name(name)
        path = _scripts_dir(file_system) / fname
        if not path.exists():
            return ActionResult(
                error=f"No script named '{fname}'. Write it first with write_code_file."
            )
        code = path.read_text()

        if url:
            try:
                await _SandboxBrowser(browser_session, clipboard).navigate(url)
            except Exception as e:
                return ActionResult(
                    error=f"navigate to {url} failed: {type(e).__name__}: {e}"
                )

        async def _save_json(obj: Any, name: str = "output.json") -> str:
            fn = _normalise_fs_name(name, "json")
            await file_system.write_file(fn, json.dumps(obj, indent=2, default=str))
            return fn

        async def _read_json(name: str) -> Any:
            fn = _normalise_fs_name(name, "json")
            file_obj = file_system.get_file(fn) or file_system.get_file(name)
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

        if url:
            clipboard.setdefault("_visited", set()).add(_norm_url(url))

        namespace.update(
            {
                "browser": _SandboxBrowser(browser_session, clipboard),
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
        return await _exec_in_sandbox(code, namespace)


def register_clipboard_tools(tools: Tools, clipboard: dict[str, Any]) -> None:
    """Register a per-session key/value clipboard (shared with the sandbox's
    ``remember``/``recall``) so the agent can stash URLs, IDs and counts and
    return to them after detours.
    """

    @tools.action(
        "Save a value to the session clipboard under a key so you can return to it "
        "later (e.g. a listings URL, an id, a running count). Persists across steps "
        "and is shared with the code sandbox (remember/recall)."
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
        self._last_loaded_url: str | None = None

    async def _new_page(self, url: str, background: bool) -> str | None:
        """Create a real new tab via CDP and return its target id.

        @nonobvious(forced-by): dispatching ``NavigateToUrlEvent(new_tab=True)`` cannot
        fan out — browser-use rewrites new_tab->False whenever the current tab is a
        new-tab page, so calls 2..N re-navigate the SAME blank tab. Target.createTarget
        makes a distinct target every time. A TabCreatedEvent is emitted afterwards so
        the watchdogs and session manager track it, mirroring browser-use's own sequence.
        """
        try:
            target_id = await self._session._cdp_create_new_page(url, background=background)
        except Exception:
            logger.debug("_new_page: _cdp_create_new_page failed", exc_info=True)
            return None
        try:
            evt = self._session.event_bus.dispatch(
                TabCreatedEvent(target_id=target_id, url=url)
            )
            await evt
            await evt.event_result(raise_if_any=False, raise_if_none=False)
        except Exception:
            logger.debug("_new_page: TabCreatedEvent dispatch failed", exc_info=True)
        return target_id

    async def _open_blank(self) -> str | None:
        return await self._new_page("about:blank", background=True)

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
        self._last_loaded_url = url
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

        target_id = await self._new_page(abs_url, background=True)
        if not target_id:
            return f"Could not open a new tab for {abs_url}."

        n = len(self._urls)
        self._urls.append(abs_url)
        self._target_ids.append(target_id)
        reverted = await self._track_loaded(n)

        await self._switch(target_id)
        self._last_loaded_url = abs_url
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


def register_tab_tools(
    tools: Tools, tab_manager: TabManager, clipboard: dict[str, Any]
) -> None:
    """Register the lazy multi-tab fan-out actions on a Tools instance."""

    @tools.action(
        "Queue URLs as lightweight, UNLOADED background tabs for efficient fan-out "
        "(hard cap 48 total). Each becomes a blank about:blank tab at a stable 0-based "
        "index; the real URL is only fetched when you call goto_tab(n). Call with NO "
        "urls to open every link from your last find_links (saved as found_links) — the "
        "one-shot way to open a whole listing. Your current/start tab is left untouched."
    )
    async def open_tabs(urls: list[str] | None = None) -> ActionResult:
        try:
            if not urls:
                urls = list(clipboard.get("found_links") or [])
                if not urls:
                    return ActionResult(
                        error="No urls given and no saved found_links — run find_links first."
                    )
            note = await tab_manager.open_tabs(urls)
            note += (
                " Next: walk them — goto_tab(0), read the detail page, update_item that "
                "role, then goto_tab(1), and so on. Or run one extraction script across "
                "the saved links. Do NOT add items from the listing alone."
            )
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
            if tab_manager._last_loaded_url:
                clipboard.setdefault("_visited", set()).add(
                    _norm_url(tab_manager._last_loaded_url)
                )
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
            if tab_manager._last_loaded_url:
                clipboard.setdefault("_visited", set()).add(
                    _norm_url(tab_manager._last_loaded_url)
                )
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
        "Collect links (index, text, href) from the current page using a selector "
        "(one or more REQUIRED): href_contains / href_regex match the URL; "
        "frame_url_contains returns only links inside an embedded panel/iframe whose "
        "URL matches (e.g. 'ashby'); container_index returns only links inside that "
        "element (usually an embed's own index); attr returns links carrying a shared "
        "attribute, e.g. {\"class\": \"posting\"}. Multiple selectors narrow together. "
        "This is the ONLY tool that can read links inside embedded/cross-origin panels. "
        "The result is saved as found_links, so open them ALL with open_tabs() (no args) "
        "or one with open_in_new_tab(index) — no need to copy hrefs back."
    )
    async def find_links(
        browser_session: BrowserSession,
        file_system: FileSystem,
        href_contains: str | None = None,
        href_regex: str | None = None,
        frame_url_contains: str | None = None,
        container_index: int | None = None,
        attr: dict[str, str] | None = None,
        visible_only: bool = False,
    ) -> ActionResult:
        if not (
            href_contains
            or href_regex
            or frame_url_contains
            or container_index is not None
            or attr
        ):
            return ActionResult(
                error="find_links needs at least one selector: href_contains, "
                "href_regex, frame_url_contains, container_index, or attr."
            )
        try:
            selector_map = await browser_session.get_selector_map()
            current = await _eval_js(browser_session, "window.location.href") or ""

            frame_target_ids: set[Any] | None = None
            if frame_url_contains:
                needle = frame_url_contains.lower()
                frame_target_ids = set()
                for node in selector_map.values():
                    if node.tag_name != "iframe":
                        continue
                    cd = getattr(node, "content_document", None)
                    src = (node.attributes or {}).get("src", "") or ""
                    if cd is not None and needle in src.lower():
                        frame_target_ids.add(cd.target_id)

            container_target_id = None
            container_ident: tuple | None = None
            if container_index is not None:
                container = await browser_session.get_element_by_index(container_index)
                if container is None:
                    return ActionResult(error=f"No element at index {container_index}.")
                cd = getattr(container, "content_document", None)
                if container.tag_name == "iframe" and cd is not None:
                    container_target_id = cd.target_id
                else:
                    container_ident = (container.session_id, container.backend_node_id)

            pattern = re.compile(href_regex) if href_regex else None

            def _in_container(node: Any) -> bool:
                if container_target_id is not None:
                    return node.target_id == container_target_id
                cur, seen = node, 0
                while cur is not None and seen < 300:
                    if (cur.session_id, cur.backend_node_id) == container_ident:
                        return True
                    cur = cur.parent_node
                    seen += 1
                return False

            links: list[dict[str, Any]] = []
            seen_href: set[str] = set()
            for index in sorted(selector_map):
                node = selector_map[index]
                if node.tag_name != "a":
                    continue
                if visible_only and not node.is_visible:
                    continue
                href = (node.attributes or {}).get("href")
                if not href:
                    continue
                abs_href = urljoin(current, href)
                if href_contains and href_contains.lower() not in abs_href.lower():
                    continue
                if pattern and not pattern.search(abs_href):
                    continue
                if frame_target_ids is not None and node.target_id not in frame_target_ids:
                    continue
                if container_index is not None and not _in_container(node):
                    continue
                if attr and not all(
                    k in (node.attributes or {})
                    and str(v).lower() in str((node.attributes or {}).get(k, "")).lower()
                    for k, v in attr.items()
                ):
                    continue
                if abs_href in seen_href:
                    continue
                seen_href.add(abs_href)
                links.append(
                    {
                        "index": index,
                        "text": node.get_meaningful_text_for_llm()[:150],
                        "href": abs_href,
                    }
                )
        except Exception as e:
            return ActionResult(error=f"find_links failed: {type(e).__name__}: {e}")

        clipboard["found_links"] = [link["href"] for link in links]
        saved: str | None = "found_links.json"
        try:
            await file_system.write_file(saved, json.dumps(links, indent=2))
        except Exception:
            logger.warning("find_links: failed to save found_links.json", exc_info=True)
            saved = None
        frame_hint = ""
        if frame_url_contains:
            frame_hint = (
                f" In a script, read each detail page's embed with "
                f"browser.frame_text('{frame_url_contains}') — reuse the SAME "
                f"'{frame_url_contains}' you matched here; do not look up the iframe's "
                "exact src."
            )
        pointer = (
            f"find_links found {len(links)} link(s), saved as found_links"
            + (f" and {saved}" if saved else "")
            + ". Next: call open_tabs() with no args to open them ALL, then walk the "
            "tabs one by one — goto_tab(n), read the detail page, add_item or "
            "update_item with what it shows, close_tab — because each item's detail "
            "(description, posted date and more) lives on its own page, not this "
            "listing. Or write ONE extraction script and run it across the links, then "
            "add_items_from_file." + frame_hint + " The links stay in view below and via "
            "recall('found_links') — no need to re-read."
        )
        return ActionResult(
            extracted_content=json.dumps(links, indent=2),
            long_term_memory=pointer,
        )


_GUARDED_DUMP_ACTIONS = (
    "find_elements",
    "evaluate",
    "find_links",
    "http_fetch",
    "run_code_file",
)
_GUARDED_DEDUP_ACTIONS = ("read_output", "search_output")


def register_output_guard_overrides(tools: Tools) -> None:
    """Stop a run's context ballooning: replace any large chunk already seen this
    session with a short back-reference, and (for dump actions only) cap genuinely huge
    single outputs to a file. The answer-surface reads (read_output/search_output) are
    deduped but never capped, so the store always shows whole. Rewrites BOTH
    ``extracted_content`` and ``long_term_memory`` (browser-use folds
    ``long_term_memory`` into permanent context first, so capping only the former
    silently misses a whole size band).

    Swaps each action's normalised function in place — the registry executes
    ``entry.function`` at call time, and every function is normalised to accept
    ``(params, **special_context)``, so one uniform wrapper covers all of them with no
    re-registration or per-action signature. Must run AFTER every other ``register_*``
    so it wraps the final version of each action.
    """
    registry_actions = tools.registry.registry.actions
    seen: dict[str, int] = {}
    counter = {"n": 0}

    async def _guard(
        result: ActionResult, file_system: Any, readout_name: str, cap: bool
    ) -> ActionResult:
        for attr in ("extracted_content", "long_term_memory"):
            text = getattr(result, attr, None)
            if not text or len(text) <= _GUARD_MIN_CHARS:
                continue
            key = guard_key(text)
            if key in seen:
                setattr(result, attr, f"[identical to earlier output #{seen[key]} — not repeated]")
                continue
            counter["n"] += 1
            seen[key] = counter["n"]
            if cap and len(text) > _CAPPED_READ_PREVIEW_CHARS:
                total = len(text)
                tail = "narrow your query instead of dumping"
                if file_system is not None and readout_name:
                    try:
                        await file_system.write_file(readout_name, text)
                        tail = f"saved to '{readout_name}' — read specific parts instead"
                    except Exception:
                        logger.warning("output guard: failed to save readout", exc_info=True)
                setattr(
                    result,
                    attr,
                    text[:_CAPPED_READ_PREVIEW_CHARS]
                    + f"\n[truncated: {total} chars total, {tail}] (output #{seen[key]})",
                )
        return result

    def _install(name: str, cap: bool) -> None:
        entry = registry_actions.get(name)
        if entry is None:
            return
        original = entry.function
        readout = f"readout_{name}.txt"

        async def wrapped(
            params: Any = None,
            _original: Any = original,
            _readout: str = readout,
            _cap: bool = cap,
            **kwargs: Any,
        ) -> Any:
            result = await _original(params=params, **kwargs)
            if isinstance(result, ActionResult):
                return await _guard(result, kwargs.get("file_system"), _readout, _cap)
            return result

        entry.function = wrapped

    for name in _GUARDED_DUMP_ACTIONS:
        _install(name, True)
    for name in _GUARDED_DEDUP_ACTIONS:
        _install(name, False)


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


def _item_url_field(store: OutputStore) -> str | None:
    """The item field that points at its OWN detail page, so the enrichment nudge can
    name where to go. Schema-generic and priority-ordered: a source/detail/apply link
    first, then a generic link, and only a bare ``*url`` that is not a company- or
    employer-level URL last (else a ``companyUrl`` would send the agent to the wrong
    page ahead of the real ``sourceUrl``).
    """
    model = store.item_model
    if model is None:
        return None
    names = list(model.model_fields)
    for kw in ("sourceurl", "applyurl", "detailurl", "joburl", "posturl", "permalink"):
        for name in names:
            if kw in name.lower():
                return name
    for kw in ("href", "link"):
        for name in names:
            if kw in name.lower():
                return name
    for name in names:
        low = name.lower()
        if "url" in low and not any(
            x in low for x in ("company", "employer", "org", "logo", "image", "careers")
        ):
            return name
    return None


def _enrichment_note(store: OutputStore, base_msg: str, index: int) -> str:
    """Append to an add_item/update_item result the fields still empty on that item and
    a push to open its own page and fill them — this is what turns a listing stub into
    a full record instead of the finished answer.
    """
    empties = store.item_missing_fields(index)
    if not empties:
        return f"{base_msg} Every field on this item is filled."
    shown = ", ".join(empties[:12])
    if len(empties) > 12:
        shown += f", +{len(empties) - 12} more"
    url_field = _item_url_field(store)
    where = f"its {url_field}" if url_field else "its own page"
    return (
        f"{base_msg} Still empty on this item: {shown}. If you have not read this "
        f"item's own page yet, open {where} and update_item({index}, {{…}}) to fill "
        "what that page shows — detail such as a description or posted date lives on "
        "the item's page, not the listing. Leave a field blank only once you have "
        "looked there and found it genuinely absent."
    )


_MAX_UNVISITED_STUBS = 2
_STUB_CONTENT_CHARS = 120


def _item_has_substantial_content(item: dict) -> bool:
    """True if the item carries a real page-read field (a description far exceeds a
    listing's short title/location), so it is drilled-in data, not a bare listing row.
    """
    return any(
        isinstance(v, str) and len(v) > _STUB_CONTENT_CHARS for v in item.values()
    )


def _is_bare_stub(store: OutputStore, item: dict, visited: set) -> bool:
    """A bare stub is an item with a detail URL whose page has NOT been opened and which
    carries no substantial content yet — i.e. a listing row added without drilling in.
    An item with a real description, or whose URL was visited, is never a bare stub.
    """
    url_field = _item_url_field(store)
    url_val = item.get(url_field) if url_field else None
    if not url_val:
        return False
    if _item_has_substantial_content(item):
        return False
    return _norm_url(url_val) not in visited


def _bare_stub_count(store: OutputStore, visited: set) -> int:
    """How many bare stubs are already in the store — the throttle that stops the agent
    batch-adding the whole listing without opening any detail page.
    """
    if not store.array_field:
        return 0
    arr = store.data.get(store.array_field) or []
    return sum(1 for it in arr if isinstance(it, dict) and _is_bare_stub(store, it, visited))


def register_output_store_tools(
    tools: Tools, store: OutputStore, clipboard: dict[str, Any] | None = None
) -> None:
    """Expose the schema-validated output store as agent actions. The store is the
    single answer surface: the agent fills it as it discovers data, every write is
    validated live and mirrored to ``output.json``, and the final result is read
    back from it after the run. A shared ``clipboard['_visited']`` URL set throttles
    adding items whose own detail page has not been opened, so the listing cannot be
    batched in as the finished answer.
    """
    array = store.array_field or "output"

    def _visited() -> set:
        if clipboard is None:
            return set()
        return clipboard.setdefault("_visited", set())

    def _stub_block(item: dict[str, Any]) -> str | None:
        visited = _visited()
        if not _is_bare_stub(store, item, visited):
            return None
        if _bare_stub_count(store, visited) < _MAX_UNVISITED_STUBS:
            return None
        return (
            f"Slow down — you already have {_MAX_UNVISITED_STUBS} listing stubs with no "
            "detail. Open THIS role's page before adding more: goto_tab on its queued "
            "tab, or navigate to its URL in a script and read the embed with "
            "browser.frame_text, then add_item / update_item with the description. Do "
            "not batch items in from the listing."
        )

    @tools.action(
        f"Append one item to the '{array}' list — the primary answer array. "
        f"{_describe_item_fields(store)} Provide every field you already know now; "
        "enrich the rest with update_item once you have opened the item's own page. "
        "Validated against the schema and rejected if it does not fit. You may hold at "
        "most two items whose own page you have not opened yet — open them before adding more."
    )
    async def add_item(item: dict[str, Any], file_system: FileSystem) -> ActionResult:
        block = _stub_block(item)
        if block:
            return ActionResult(error=block)
        ok, msg = store.add_item(item)
        if not ok:
            return ActionResult(error=msg)
        await _mirror_output(store, file_system)
        note = _enrichment_note(store, msg, store.item_count() - 1)
        return ActionResult(extracted_content=note, long_term_memory=note)

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
        note = _enrichment_note(store, msg, int(index))
        return ActionResult(extracted_content=note, long_term_memory=note)

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

    @tools.action(
        f"Bulk-load items into the '{array}' list from a JSON array file you saved "
        "(e.g. save_json(rows, 'jobs.json') at the end of an extraction script): "
        "validates each element against the schema and appends them in ONE step, "
        "reporting per-index failures. The fast way to fill the output after a script "
        "has read every role's own page. Items whose page you have not opened are "
        "skipped — open them first."
    )
    async def add_items_from_file(name: str, file_system: FileSystem) -> ActionResult:
        fn = _normalise_fs_name(name, "json")
        file_obj = file_system.get_file(fn) or file_system.get_file(name)
        if file_obj is None:
            return ActionResult(
                error=f"No file named {name!r}. Save it first with save_json in a script."
            )
        try:
            arr = json.loads(file_obj.read())
        except Exception as e:
            return ActionResult(error=f"{name} is not valid JSON: {e}")
        if not isinstance(arr, list):
            return ActionResult(error=f"{name} must contain a JSON array of items.")

        added = 0
        failures: list[str] = []
        blocked = 0
        for i, it in enumerate(arr):
            if not isinstance(it, dict):
                failures.append(f"#{i}: not an object")
                continue
            if _stub_block(it):
                blocked += 1
                continue
            ok, msg = store.add_item(it)
            if ok:
                added += 1
            else:
                failures.append(f"#{i}: {msg}")
        await _mirror_output(store, file_system)
        parts = [f"Added {added} of {len(arr)} items from {fn}."]
        if failures:
            parts.append("Rejected: " + "; ".join(failures[:5]))
        if blocked:
            parts.append(
                f"{blocked} skipped because their own pages were not opened — visit them "
                "(or navigate to them in your script) before loading."
            )
        note = " ".join(parts)
        return ActionResult(extracted_content=note, long_term_memory=note)


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
