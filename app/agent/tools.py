"""Custom browser-use tools — Capsolver CAPTCHA solving, Python sandbox, HTTP fetch."""

import asyncio
import html as html_lib
import json
import logging
import re
from collections import Counter
from pathlib import Path
from typing import Any, get_args, get_origin
from urllib.parse import urljoin, urlparse

import httpx
from pydantic import BaseModel, TypeAdapter

from browser_use import ActionResult, BrowserSession, Tools
from browser_use.browser.events import (
    CloseTabEvent,
    NavigateToUrlEvent,
    SwitchTabEvent,
    TabCreatedEvent,
)
from browser_use.filesystem.file_system import FileSystem

from app.agent.output_store import (
    OutputStore,
    _coerce_scalar,
    _name_tokens,
    _peel_optional,
    elide_long_values,
)
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
    so ``…?id=X#section`` and the same URL without the fragment compare equal.
    """
    u = (url or "").strip().lower().split("#", 1)[0]
    return u.rstrip("/")


_BODY_TEXT_JS = "document.body ? document.body.innerText : ''"
_JSONLD_JS = (
    "(function(){var out=[];document.querySelectorAll("
    "'script[type=\"application/ld+json\"]').forEach(function(s){"
    "out.push(s.textContent)});return out;})()"
)
_LINKS_JS = (
    "(function(){var out=[];document.querySelectorAll('a[href]').forEach(function(a){"
    "if(out.length<80)out.push({text:(a.innerText||'').trim().slice(0,120),href:a.href})"
    "});return out;})()"
)
_IFRAME_SRC_JS = (
    "(function(){var out=[];document.querySelectorAll('iframe').forEach(function(f){"
    "if(f.src)out.push(f.src)});return out;})()"
)


_JSONLD_BOILERPLATE_TYPES = (
    "Organization",
    "WebSite",
    "WebPage",
    "BreadcrumbList",
    "SearchAction",
    "SiteNavigationElement",
)


def _parse_jsonld_blobs(raw_list: Any) -> Any:
    """Parse raw JSON-LD script contents and pick the best object: the first dict
    whose @type is NOT site boilerplate (a Product, Event, Article, …
    over the page's Organization/WebSite/BreadcrumbList chrome), else the first
    parseable object, else None. Handles a top-level list wrapping the real object.
    """
    parsed: list[Any] = []
    if isinstance(raw_list, list):
        for raw in raw_list:
            try:
                obj = json.loads(raw)
            except Exception:
                continue
            if isinstance(obj, list):
                parsed.extend(obj)
            else:
                parsed.append(obj)
    for obj in parsed:
        if not isinstance(obj, dict):
            continue
        obj_type = str(obj.get("@type", ""))
        if obj_type and not any(b in obj_type for b in _JSONLD_BOILERPLATE_TYPES):
            return obj
    return parsed[0] if parsed else None


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


async def _eval_on_target(
    browser_session: BrowserSession, target_id: str, expression: str
) -> Any:
    """Runtime.evaluate against a specific CDP target (a background tab or an OOPIF)."""
    sess = await browser_session.get_or_create_cdp_session(target_id, focus=False)
    result = await sess.cdp_client.send.Runtime.evaluate(
        params={"expression": expression, "returnByValue": True, "awaitPromise": True},
        session_id=sess.session_id,
    )
    if result.get("exceptionDetails"):
        raise RuntimeError(f"JS error: {result['exceptionDetails']}")
    return result.get("result", {}).get("value")


async def _spawn_tab(browser_session: BrowserSession, url: str) -> str | None:
    """Create a real background tab via CDP and return its target id, emitting the
    TabCreatedEvent browser-use's watchdogs expect.

    @nonobvious(forced-by): dispatching ``NavigateToUrlEvent(new_tab=True)`` cannot
    fan out — browser-use rewrites new_tab->False whenever the current tab is a
    new-tab page, so calls 2..N re-navigate the SAME blank tab. Target.createTarget
    makes a distinct target every time.
    """
    try:
        target_id = await browser_session._cdp_create_new_page(url, background=True)
    except Exception:
        logger.debug("_spawn_tab: _cdp_create_new_page failed", exc_info=True)
        return None
    try:
        evt = browser_session.event_bus.dispatch(
            TabCreatedEvent(target_id=target_id, url=url)
        )
        await evt
        await evt.event_result(raise_if_any=False, raise_if_none=False)
    except Exception:
        logger.debug("_spawn_tab: TabCreatedEvent dispatch failed", exc_info=True)
    return target_id


async def _close_spawned_tab(browser_session: BrowserSession, target_id: str) -> None:
    try:
        evt = browser_session.event_bus.dispatch(CloseTabEvent(target_id=target_id))
        await evt
        await evt.event_result(raise_if_any=False, raise_if_none=False)
    except Exception:
        logger.debug("_close_spawned_tab failed", exc_info=True)


async def _focus_target(browser_session: BrowserSession, target_id: str | None) -> None:
    if not target_id:
        return
    try:
        evt = browser_session.event_bus.dispatch(SwitchTabEvent(target_id=target_id))
        await evt
        await evt.event_result(raise_if_any=False, raise_if_none=False)
    except Exception:
        logger.debug("_focus_target failed", exc_info=True)


async def _iframe_targets(browser_session: BrowserSession) -> list[dict[str, str]]:
    cdp = await browser_session.get_or_create_cdp_session()
    targets = await cdp.cdp_client.send.Target.getTargets()
    return [
        {"targetId": t["targetId"], "url": t.get("url", "")}
        for t in targets.get("targetInfos", [])
        if t.get("type") == "iframe"
    ]


def _url_discriminators(url: str) -> set[str]:
    """Long, distinctive tokens from a page URL (query values and path segments)
    that can re-identify the page's own embed among many — an embedded panel's URL
    typically carries the record id from its host page's URL (e.g. a detail page
    ``…?id=<uuid>`` framing ``…/vendor/<uuid>?embed=js``).
    """
    from urllib.parse import parse_qsl, urlparse

    try:
        parsed = urlparse(url or "")
    except Exception:
        return set()
    tokens = {value for _, value in parse_qsl(parsed.query) if len(value) >= 8}
    tokens |= {seg for seg in parsed.path.split("/") if len(seg) >= 8}
    return {t.lower() for t in tokens}


async def _match_frame_target(
    browser_session: BrowserSession,
    page_target_id: str,
    url_contains: str,
    claimed: set[str],
    baseline: set[str],
    allow_sole_candidate: bool = False,
    page_url: str | None = None,
) -> str | None:
    """The OOPIF target belonging to ``page_target_id`` whose URL contains
    ``url_contains``. Ownership is resolved by matching the global iframe-target
    list against the page's own frame tree and iframe srcs, because CDP's target
    list carries no parent linkage. The sole-unclaimed-candidate fallback is only
    honoured when ``allow_sole_candidate`` (single-page reads) — in a concurrent
    wave a slow page's sole candidate could be a SIBLING page's embed, which would
    silently attribute the wrong item's data.
    """
    needle = (url_contains or "").lower()
    tree_urls: set[str] = set()
    try:
        sess = await browser_session.get_or_create_cdp_session(
            page_target_id, focus=False
        )
        tree = await sess.cdp_client.send.Page.getFrameTree(
            session_id=sess.session_id
        )
        stack = [tree.get("frameTree")]
        while stack:
            node = stack.pop()
            if not node:
                continue
            frame_url = (node.get("frame") or {}).get("url", "")
            if frame_url:
                tree_urls.add(frame_url)
            stack.extend(node.get("childFrames") or [])
    except Exception:
        logger.debug("_match_frame_target: getFrameTree failed", exc_info=True)
    srcs: list[str] = []
    try:
        srcs = await _eval_on_target(browser_session, page_target_id, _IFRAME_SRC_JS) or []
    except Exception:
        logger.debug("_match_frame_target: iframe src read failed", exc_info=True)

    discriminators = _url_discriminators(page_url or "")
    candidates: list[dict[str, str]] = []
    for t in await _iframe_targets(browser_session):
        tid, turl = t["targetId"], t["url"]
        if tid in claimed:
            continue
        if needle and needle not in turl.lower():
            continue
        low = turl.lower()
        if any(token in low for token in discriminators):
            return tid
        if turl in tree_urls:
            return tid
        for src in srcs:
            base = str(src).split("#", 1)[0]
            if base and (turl == src or turl.startswith(base)):
                return tid
        if tid not in baseline:
            candidates.append(t)
    if allow_sole_candidate and len(candidates) == 1:
        return candidates[0]["targetId"]
    return None


_COUNT_LINKS_JS = "document.querySelectorAll('a[href]').length"
_SCROLL_BOTTOM_JS = "window.scrollTo(0, document.body ? document.body.scrollHeight : 0)"
_LAZY_MAX_ROUNDS = 8
_LAZY_POLL_S = 0.6


async def _settle_lazy_links(
    browser_session: BrowserSession, frame_url_contains: str | None
) -> None:
    """Coax a lazily-populating listing into showing everything before links are
    collected: repeatedly scroll the main page and any matching embedded frame to
    the bottom, and only proceed once the link count has stopped growing for two
    consecutive polls. Listings (and their embeds) commonly append items on scroll
    or a second after first paint, so collecting immediately under-counts. The main
    page's scroll position is restored afterwards.
    """
    needle = (frame_url_contains or "").lower()

    async def _matching_frames() -> list[str]:
        if not needle:
            return []
        return [
            t["targetId"]
            for t in await _iframe_targets(browser_session)
            if needle in t["url"].lower()
        ]

    async def _count() -> int:
        total = 0
        try:
            total += int(await _eval_js(browser_session, _COUNT_LINKS_JS) or 0)
        except Exception:
            logger.debug("_settle_lazy_links: main count failed", exc_info=True)
        for tid in await _matching_frames():
            try:
                total += int(
                    await _eval_on_target(browser_session, tid, _COUNT_LINKS_JS) or 0
                )
            except Exception:
                logger.debug("_settle_lazy_links: frame count failed", exc_info=True)
        return total

    async def _scroll_all() -> None:
        try:
            await _eval_js(browser_session, _SCROLL_BOTTOM_JS)
        except Exception:
            logger.debug("_settle_lazy_links: main scroll failed", exc_info=True)
        for tid in await _matching_frames():
            try:
                await _eval_on_target(browser_session, tid, _SCROLL_BOTTOM_JS)
            except Exception:
                logger.debug("_settle_lazy_links: frame scroll failed", exc_info=True)

    original_y = 0
    try:
        original_y = int(await _eval_js(browser_session, "window.scrollY") or 0)
    except Exception:
        logger.debug("_settle_lazy_links: scrollY read failed", exc_info=True)

    stable = 0
    last = await _count()
    for _ in range(_LAZY_MAX_ROUNDS):
        await _scroll_all()
        await asyncio.sleep(_LAZY_POLL_S)
        current = await _count()
        if current == last:
            stable += 1
            if stable >= 2:
                break
        else:
            stable = 0
            last = current

    try:
        await _eval_js(browser_session, f"window.scrollTo(0, {original_y})")
    except Exception:
        logger.debug("_settle_lazy_links: scroll restore failed", exc_info=True)


_READ_PAGES_MAX = 48
_READ_PAGES_TEXT_CAP = 60_000
# @nonobvious(mirrors): must stay below BROWSER_USE_ACTION_TIMEOUT_S (set in
# app/agent/runner.py) which must stay below the agent step_timeout there —
# read_pages stops itself gracefully; the outer caps must never fire first.
_READ_PAGES_BUDGET_S = 420.0
_READ_PAGES_MIN_WAVE_S = 30.0
# @nonobvious(means): measured live — a heavy marketing page took ~12s to DOMContentLoaded
# with its embed rendering ~2.5s later, so 18s left almost no margin.
_PAGE_READY_TIMEOUT_S = 25.0
_MIN_PAGE_TEXT_CHARS = 200
_JSONLD_GRACE_S = 3.0


async def _read_one_page(
    browser_session: BrowserSession,
    url: str,
    target_id: str,
    url_contains: str | None,
    claimed: set[str],
    baseline: set[str],
    allow_sole_candidate: bool = False,
) -> dict[str, Any]:
    """Wait for a spawned tab (and, when asked, its embedded panel) to render, then
    read {url, title, text, jsonld, links} from it — the panel when one matches,
    else the main document. Rendering only counts once the text is substantial
    (embeds paint a thin loading shell first), and a page whose JSON-LD has not
    arrived with the text gets a short grace poll — that is where posted dates and
    other structured details live, so reading the shell would silently null those fields.
    """
    page: dict[str, Any] = {"url": url}
    frame_tid: str | None = None
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _PAGE_READY_TIMEOUT_S

    def _substantial(txt: Any) -> bool:
        return bool(txt) and len(str(txt).strip()) >= _MIN_PAGE_TEXT_CHARS

    while loop.time() < deadline:
        try:
            if url_contains:
                frame_tid = await _match_frame_target(
                    browser_session,
                    target_id,
                    url_contains,
                    claimed,
                    baseline,
                    allow_sole_candidate,
                    page_url=url,
                )
                if frame_tid:
                    txt = await _eval_on_target(browser_session, frame_tid, _BODY_TEXT_JS)
                    if _substantial(txt):
                        break
            else:
                ready = await _eval_on_target(
                    browser_session, target_id, "document.readyState"
                )
                if ready in ("interactive", "complete"):
                    txt = await _eval_on_target(browser_session, target_id, _BODY_TEXT_JS)
                    if _substantial(txt):
                        break
        except Exception:
            logger.debug("_read_one_page: poll failed", exc_info=True)
        await asyncio.sleep(0.5)

    read_tid = frame_tid or target_id
    if frame_tid:
        claimed.add(frame_tid)
        page["frame_matched"] = True
    try:
        page["title"] = await _eval_on_target(browser_session, target_id, "document.title")
        text = await _eval_on_target(browser_session, read_tid, _BODY_TEXT_JS) or ""
        page["text"] = text[:_READ_PAGES_TEXT_CAP]

        async def _jsonld_now() -> Any:
            found = _parse_jsonld_blobs(
                await _eval_on_target(browser_session, read_tid, _JSONLD_JS)
            )
            if found is None and read_tid != target_id:
                found = _parse_jsonld_blobs(
                    await _eval_on_target(browser_session, target_id, _JSONLD_JS)
                )
            return found

        jsonld = await _jsonld_now()
        arrived_late = False
        grace_deadline = loop.time() + _JSONLD_GRACE_S
        while jsonld is None and page["text"].strip() and loop.time() < grace_deadline:
            await asyncio.sleep(0.5)
            jsonld = await _jsonld_now()
            arrived_late = jsonld is not None
        page["jsonld"] = jsonld
        if arrived_late:
            text = await _eval_on_target(browser_session, read_tid, _BODY_TEXT_JS) or ""
            if len(text) > len(page["text"]):
                page["text"] = text[:_READ_PAGES_TEXT_CAP]
        page["links"] = await _eval_on_target(browser_session, read_tid, _LINKS_JS) or []
    except Exception as e:
        page["error"] = f"{type(e).__name__}: {e}"
        return page
    if url_contains and not frame_tid:
        page["error"] = (
            "no embedded panel matching "
            f"'{url_contains}' rendered — the main document was NOT read in its place"
        )
    elif not (page.get("text") or "").strip():
        page["error"] = "no readable text rendered"
    return page


async def _emit_progress(progress: Any, message: str) -> None:
    if progress is None:
        return
    try:
        await progress(message)
    except Exception:
        logger.debug("_emit_progress failed", exc_info=True)


def _saved_links_sans_offhost(clipboard: dict[str, Any] | None) -> tuple[list[str], int]:
    """The last find_links result minus links flagged as pointing off-site, plus
    how many were skipped — so a no-args bulk read covers the listing without
    dragging in navigation/branding pages.
    """
    cb = clipboard or {}
    urls = list(cb.get("found_links") or [])
    off = cb.get("found_links_offhost") or set()
    kept = [u for u in urls if u not in off]
    return kept, len(urls) - len(kept)


async def _read_pages_impl(
    browser_session: BrowserSession,
    urls: list[str],
    url_contains: str | None,
    clipboard: dict[str, Any] | None,
    concurrency: int = 6,
    progress: Any = None,
) -> list[dict[str, Any]]:
    """Read many pages concurrently: open a wave of tabs so they load in parallel,
    then focus and read each in turn (visible in the live view, like a human walking
    their opened tabs), close the wave, repeat. Failures are retried once one at a
    time, and a page whose JSON-LD is missing while sibling pages produced JSON-LD
    gets one hard re-navigate too — a slow injection needs a fresh load, not a
    longer stare. Focus returns to the starting tab at the end; read URLs are
    marked visited and dead ones recorded as read-failures.
    """
    concurrency = max(1, min(int(concurrency or 6), 8))
    baseline = {t["targetId"] for t in await _iframe_targets(browser_session)}
    home_target = getattr(browser_session, "agent_focus_target_id", None)
    results: dict[str, dict[str, Any]] = {}
    loop = asyncio.get_running_loop()
    budget_deadline = loop.time() + _READ_PAGES_BUDGET_S

    async def _run_wave(wave: list[str]) -> None:
        pairs: list[tuple[str, str]] = []
        try:
            for u in wave:
                tid = await _spawn_tab(browser_session, u)
                if tid is None:
                    results[u] = {"url": u, "error": "could not open a tab"}
                else:
                    pairs.append((u, tid))
            claimed: set[str] = set()
            for u, tid in pairs:
                await _focus_target(browser_session, tid)
                results[u] = await _read_one_page(
                    browser_session,
                    u,
                    tid,
                    url_contains,
                    claimed,
                    baseline,
                    allow_sole_candidate=len(pairs) == 1,
                )
        finally:
            # @nonobvious(forced-by): closing a FOCUSED target fires browser-use's
            # focus-detach auto-recovery, which can race the next close and wedge
            # the session into 'browser not connected' (observed live) — so focus
            # home before closing, and close in a shielded finally so a cancelled
            # wave never orphans its tabs.
            await asyncio.shield(_focus_target(browser_session, home_target))
            for _, tid in pairs:
                await asyncio.shield(_close_spawned_tab(browser_session, tid))

    def _out_of_budget(pending: list[str]) -> bool:
        if loop.time() + _READ_PAGES_MIN_WAVE_S <= budget_deadline:
            return False
        for u in pending:
            if u not in results or results[u].get("error"):
                results[u] = {
                    "url": u,
                    "error": (
                        "not attempted — read_pages stopped before its time budget "
                        "expired; call read_pages again with the remaining urls"
                    ),
                }
        return True

    try:
        total_waves = (len(urls) + concurrency - 1) // concurrency
        await _emit_progress(
            progress,
            f"read_pages: {len(urls)} page(s) in {total_waves} wave(s) of up to {concurrency} tabs",
        )
        for wave_no, i in enumerate(range(0, len(urls), concurrency), start=1):
            if _out_of_budget(urls[i:]):
                await _emit_progress(progress, "read_pages: time budget reached, stopping early")
                break
            wave_started = loop.time()
            await _run_wave(urls[i : i + concurrency])
            done = [results.get(u, {}) for u in urls[: i + concurrency] if u in results]
            ok = sum(1 for p in done if not p.get("error"))
            await _emit_progress(
                progress,
                f"read_pages wave {wave_no}/{total_waves}: {ok} of {len(done)} pages ok, "
                f"{sum(1 for p in done if p.get('frame_matched'))} frames matched "
                f"({loop.time() - wave_started:.0f}s)",
            )

        retry = [u for u in urls if results.get(u, {}).get("error")]
        if retry:
            await _emit_progress(
                progress, f"read_pages: retrying {len(retry)} failed page(s) one at a time"
            )
        for u in retry:
            if _out_of_budget([u]):
                break
            await _run_wave([u])

        any_jsonld = any(
            p.get("jsonld") for p in results.values() if not p.get("error")
        )
        if any_jsonld:
            for u in urls:
                page = results.get(u) or {}
                if page.get("error") or page.get("jsonld"):
                    continue
                if not (page.get("text") or "").strip():
                    continue
                if loop.time() + _READ_PAGES_MIN_WAVE_S > budget_deadline:
                    break
                before = results[u]
                await _run_wave([u])
                after = results.get(u) or {}
                if after.get("error") or not after.get("jsonld"):
                    results[u] = before
    finally:
        await _focus_target(browser_session, home_target)

    shell_flagged, embed_hosts = await _flag_shell_reads(browser_session, results)
    if shell_flagged:
        await _emit_progress(
            progress,
            f"read_pages: {shell_flagged} page(s) returned the embedding shell, "
            "not real content — retrying inside the embedded panel",
        )
    if shell_flagged and embed_hosts:
        # @nonobvious(forced-by): the platform retries inside the embed itself
        # rather than instructing the model to — models route around a failed
        # read using stale files instead of re-reading, so an instruction-based
        # retry never happens in practice.
        url_contains = embed_hosts[0]
        flagged_urls = [
            u
            for u in urls
            if "embedding shell" in (results.get(u, {}).get("error") or "")
        ]
        try:
            for i in range(0, len(flagged_urls), concurrency):
                if _out_of_budget(flagged_urls[i:]):
                    break
                await _run_wave(flagged_urls[i : i + concurrency])
        finally:
            await _focus_target(browser_session, home_target)
        await _flag_shell_reads(browser_session, results)
        recovered = sum(
            1 for u in flagged_urls if not results.get(u, {}).get("error")
        )
        if recovered and clipboard is not None and not clipboard.get("found_links_frame"):
            clipboard["found_links_frame"] = url_contains
        await _emit_progress(
            progress,
            f"read_pages: retry inside '{url_contains}' recovered "
            f"{recovered} of {len(flagged_urls)} page(s)",
        )

    if clipboard is not None:
        visited = clipboard.setdefault("_visited", set())
        failed = clipboard.setdefault("_read_failed", set())
        listing_meta = clipboard.get("found_links_meta") or {}
        for u, page in results.items():
            if listing_meta.get(u):
                page.setdefault("listing_text", listing_meta[u])
            if page.get("error"):
                failed.add(_norm_url(u))
            else:
                visited.add(_norm_url(u))
        _extend_evidence_corpus(clipboard, results)
    return [results[u] for u in urls if u in results]


async def _flag_shell_reads(
    browser_session: BrowserSession, results: dict[str, dict[str, Any]]
) -> tuple[int, list[str]]:
    """Turn silent shell reads into loud failures: when several pages came back
    with near-identical text, none matched an embedded panel, and cross-origin
    embeds exist, the reads captured the embedding page rather than each page's
    real content — reporting them "ok" would let the whole downstream pipeline
    run on marketing boilerplate. Returns how many pages were flagged and the
    embed hosts a retry should target.
    """
    ok_pages = [p for p in results.values() if not p.get("error")]
    if len(ok_pages) < 3 or any(p.get("frame_matched") for p in ok_pages):
        return 0, []

    def _sig(page: dict[str, Any]) -> str:
        return " ".join((page.get("text") or "").split())[:1500]

    top_sig, top_n = Counter(_sig(p) for p in ok_pages).most_common(1)[0]
    if not top_sig or top_n < 3:
        return 0, []
    try:
        embed_hosts = sorted(
            {
                urlparse(t.get("url") or "").netloc
                for t in await _iframe_targets(browser_session)
                if urlparse(t.get("url") or "").netloc
            }
        )
    except Exception:
        embed_hosts = []
    if not embed_hosts:
        return 0, []
    flagged = 0
    for p in ok_pages:
        if _sig(p) == top_sig:
            p["error"] = (
                "read the embedding shell, not this page's real content — "
                f"{top_n} pages returned identical text and no embedded panel was "
                "matched. The content lives inside a cross-origin embed; re-run "
                "read_pages with frame_url_contains matching one of: "
                + ", ".join(embed_hosts)
            )
            flagged += 1
    return flagged, embed_hosts


def _pages_for_save(pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Failed pages persist as {url, error} only — keeping a failed read's text
    on disk invites scripts to reuse the very content the failure disowned.
    """
    return [
        {"url": p.get("url"), "error": p["error"]} if p.get("error") else p
        for p in pages
    ]


def _norm_evidence(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def _evidence_contains(corpus: str, value: str) -> bool:
    """True when a value's letters appear somewhere in the gathered page text.
    An empty corpus always passes — with nothing read there is nothing to judge.
    Errs toward allowing (substring match over collapsed text), because the goal
    is blocking values NO page plausibly states, not spelling enforcement.
    """
    if not corpus:
        return True
    needle = re.sub(r"[^a-z0-9]+", "", (value or "").lower())
    if not needle:
        return True
    return needle in corpus.replace(" ", "")


def _extend_evidence_corpus(
    clipboard: dict[str, Any], results: dict[str, dict[str, Any]]
) -> None:
    parts = [clipboard.get("_evidence_corpus", "")]
    for page in results.values():
        if page.get("error"):
            continue
        parts.append(_norm_evidence(page.get("text") or ""))
        if page.get("jsonld"):
            parts.append(_norm_evidence(json.dumps(page["jsonld"], default=str)))
    for txt in (clipboard.get("found_links_meta") or {}).values():
        parts.append(_norm_evidence(txt))
    clipboard["_evidence_corpus"] = " ".join(p for p in parts if p)[-2_000_000:]


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
        can read an embedded/cross-origin panel (e.g. a listing detail inside an embed).
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

    async def frame_jsonld(self, url_contains: str) -> Any:
        """Parsed JSON-LD structured data from the matching cross-origin iframe — where a
        posted/published date and other structured fields live that are not in the visible
        text. Returns the first entity-describing object, else the first parseable
        object, else None (e.g. ``(await browser.frame_jsonld('embed'))['datePublished']``).
        """
        raw_list = await self.frame_evaluate(url_contains, _JSONLD_JS)
        return _parse_jsonld_blobs(raw_list)

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

    async def read_pages(
        self,
        urls: list[str] | None = None,
        frame_url_contains: str | None = None,
        concurrency: int = 6,
    ) -> list[dict[str, Any]]:
        """Read many pages in parallel background tabs and return
        ``[{url, title, text, jsonld, links, error?}, …]`` — the bulk way to read a
        whole listing's detail pages without navigating the current tab. With no
        urls, reads the links saved by the last find_links. When
        ``frame_url_contains`` is given, text/jsonld/links come from the matching
        embedded panel on each page.
        """
        if not urls:
            urls, _ = _saved_links_sans_offhost(self._clipboard)
            if not urls:
                raise ValueError("read_pages: no urls given and no saved found_links")
        if frame_url_contains is None:
            frame_url_contains = (self._clipboard or {}).get("found_links_frame")
        return await _read_pages_impl(
            self._session, list(urls), frame_url_contains, self._clipboard, concurrency
        )

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


class _AwaitableStr(str):
    """A plain string that also tolerates being awaited, so a helper that completes
    its work synchronously accepts both call styles — ``save_json(...)`` and
    ``await save_json(...)`` — and an un-awaited call can never silently lose work.
    """

    def __await__(self):
        async def _identity() -> str:
            return str(self)

        return _identity().__await__()


class _AwaitableDict(dict):
    def __await__(self):
        async def _identity() -> dict:
            return dict(self)

        return _identity().__await__()


class _AwaitableList(list):
    def __await__(self):
        async def _identity() -> list:
            return list(self)

        return _identity().__await__()


def _awaitable(value: Any) -> Any:
    """Wrap a plain result so both ``x = helper(...)`` and ``x = await helper(...)``
    behave identically for the sandbox's synchronous helpers.
    """
    if isinstance(value, dict):
        return _AwaitableDict(value)
    if isinstance(value, list):
        return _AwaitableList(value)
    if isinstance(value, str):
        return _AwaitableStr(value)
    return value


def _write_fs_file_sync(file_system: FileSystem, name: str, content: str) -> None:
    """Write a FileSystem file so it exists on disk IMMEDIATELY, then schedule the
    official async write to keep browser-use's in-memory file registry in step —
    an un-awaited async write used to vanish silently, taking the file with it.
    """
    (file_system.get_dir() / name).write_text(content)
    try:
        asyncio.get_running_loop().create_task(file_system.write_file(name, content))
    except Exception:
        logger.debug("_write_fs_file_sync: registry catch-up failed", exc_info=True)


def _code_tab_url(script_name: str) -> str:
    """A self-contained data: URL shown in a real tab while a sandbox script runs,
    so the live view makes code work visible instead of a seemingly frozen page.
    """
    from urllib.parse import quote

    safe = html_lib.escape(script_name)
    page = (
        "<!doctype html><title>Code</title>"
        "<body style='margin:0;height:100vh;display:flex;flex-direction:column;"
        "align-items:center;justify-content:center;background:#0d1117;color:#e6edf3;"
        "font-family:monospace'>"
        "<div style='width:44px;height:44px;border:4px solid #30363d;"
        "border-top-color:#58a6ff;border-radius:50%;animation:s 1s linear infinite'>"
        "</div><style>@keyframes s{to{transform:rotate(360deg)}}</style>"
        f"<p style='margin-top:24px;font-size:18px'>Model is running code&hellip;</p>"
        f"<p style='color:#8b949e'>{safe}</p></body>"
    )
    return "data:text/html;charset=utf-8," + quote(page)


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
        await asyncio.wait_for(_run(), timeout=300.0)
    except asyncio.TimeoutError:
        out = stdout.getvalue()
        tail = f"\n--- stdout so far ---\n{out[-2000:]}" if out else ""
        return ActionResult(
            error="Script timed out after 300 seconds. Anything you saved with "
            "save_json before the timeout is still on disk. For bulk page reads use "
            "browser.read_pages(urls, frame_url_contains) instead of a navigate "
            f"loop; otherwise process a smaller batch and continue in the next run.{tail}"
        )
    except Exception as e:
        out = stdout.getvalue()
        err_text = str(e).lower()
        hint = ""
        if "event loop is already running" in err_text:
            hint = (
                "\nHint: your code already runs inside a live event loop — write "
                "top-level await directly; never use asyncio.run() or "
                "loop.run_until_complete()."
            )
        elif "string indices must be integers" in err_text:
            hint = (
                "\nHint: you indexed a STRING with a key — you probably treated a "
                "JSON string as data. read_json() and read_output() already return "
                "parsed dicts/lists (never json.loads them); only raw text like "
                "open(...).read() or fetch(...).text needs json.loads."
            )
        elif "coroutine" in err_text or "can't be used in 'await'" in err_text:
            hint = (
                "\nHint: browser.* and fetch are async — call them with await. "
                "save_json, read_json, add_item/update_item/update_items/set_field/"
                "mark_absent, read_output, coverage, remember and recall all work "
                "with OR without await."
            )
        tail = f"\n--- stdout ---\n{out}" if out else ""
        return ActionResult(error=f"{type(e).__name__}: {e}{hint}{tail}"[:10000])

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


def register_code_tools(
    tools: Tools,
    clipboard: dict[str, Any] | None = None,
    store: "OutputStore | None" = None,
) -> None:
    """Register the browser-connected code sandbox. ``run_code_file`` saves and runs a
    script in one step against the live page, with a namespace that persists across
    runs so variables and imports carry over. When an output store exists, the
    sandbox can write to it directly (add_item/update_item/…) with the same
    validation as the store actions.

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
        "Write and run a Python script in ONE step: pass code= to save it to 'name' "
        "then execute it. Omit code= to re-run the already-saved 'name'. Pass url= to "
        "navigate there first. You rarely need a script for extraction — the "
        "read_pages ACTION already prefills rows_draft.json for "
        "add_items_from_file; scripts are for unusual transforms only. "
        "Also available: browser.evaluate(js) / browser.get_html(selector=None) for "
        "the MAIN page; browser.frames() / browser.frame_text(url_contains) / "
        "browser.frame_jsonld(url_contains) / browser.frame_evaluate(url_contains, js) "
        "/ browser.wait_for_frame(url_contains) to read INSIDE a cross-origin embed "
        "on the CURRENT tab; browser.navigate(url, wait_for=None); fetch(url, ...) -> "
        ".status_code/.text/.json() (server-side, no CORS, never a site's backend "
        "API); await save_json(obj, name) to save; read_json(name) and "
        "open('name.json') are SYNCHRONOUS (no await) for reading saved files; "
        "pick_jsonld(raw) normalises JSON-LD that may be a list; remember/"
        "recall; and, when a schema output exists, await add_item(item) / await "
        "update_item(index, fields) / await update_items([{index, fields}, ...]) / "
        "await set_field(key, value) / await mark_absent(field, reason) / "
        "read_output() (returns the output as a plain dict, like read_json) / "
        "coverage() write straight to the validated output. STDOUT "
        "is truncated to a small preview — print only counts/keys, never whole blobs. "
        "Variables persist across runs."
    )
    async def run_code_file(
        name: str,
        browser_session: BrowserSession,
        file_system: FileSystem,
        code: str | None = None,
        url: str | None = None,
    ) -> ActionResult:
        fname = _normalise_py_name(name)
        path = _scripts_dir(file_system) / fname
        if code is not None:
            try:
                path.write_text(code)
            except Exception as e:
                return ActionResult(error=f"saving script failed: {type(e).__name__}: {e}")
        if not path.exists():
            return ActionResult(
                error=f"No script named '{fname}'. Pass code= to write and run it in one step."
            )
        code = path.read_text()

        if url:
            try:
                await _SandboxBrowser(browser_session, clipboard).navigate(url)
            except Exception as e:
                return ActionResult(
                    error=f"navigate to {url} failed: {type(e).__name__}: {e}"
                )

        saved_files: list[str] = []

        def _save_json(obj: Any, name: str = "output.json") -> str:
            fn = _normalise_fs_name(name, "json")
            _write_fs_file_sync(file_system, fn, json.dumps(obj, indent=2, default=str))
            saved_files.append(fn)
            return _AwaitableStr(fn)

        def _read_json(name: str) -> Any:
            fn = _normalise_fs_name(name, "json")
            file_obj = file_system.get_file(fn) or file_system.get_file(name)
            if file_obj is not None:
                return _awaitable(json.loads(file_obj.read()))
            path = file_system.get_dir() / fn
            if path.exists():
                return _awaitable(json.loads(path.read_text()))
            raise FileNotFoundError(f"No saved file named {name!r}")

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

        class _SandboxAsyncio:
            """asyncio facade whose run()/run_until_complete execute the coroutine on
            a throwaway thread with its own loop — sandbox code already lives inside
            a running loop, where the real asyncio.run raises; models reach for it by
            trained habit regardless of guidance, so it has to just work.
            """

            def __getattr__(self, name: str) -> Any:
                return getattr(asyncio, name)

            @staticmethod
            def run(coro: Any, *, debug: Any = None) -> Any:
                import concurrent.futures

                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool_:
                    return pool_.submit(asyncio.run, coro).result()

        _builtin_open = open

        def _sandbox_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any):
            """Resolve bare relative filenames against the agent's FileSystem dir, so
            a script's plain ``open('items.json')`` finds files saved with save_json.
            """
            if isinstance(file, str) and "/" not in file and not file.startswith("."):
                candidate = file_system.get_dir() / file
                if "r" in mode and not candidate.exists():
                    fixed = file_system.get_dir() / _normalise_fs_name(file, "json")
                    if fixed.exists():
                        candidate = fixed
                return _builtin_open(candidate, mode, *args, **kwargs)
            return _builtin_open(file, mode, *args, **kwargs)

        namespace.update(
            {
                "browser": _SandboxBrowser(browser_session, clipboard),
                "fetch": _fetch,
                "save_json": _save_json,
                "save_checkpoint_json": _save_json,
                "read_json": _read_json,
                "pick_jsonld": _parse_jsonld_blobs,
                "open": _sandbox_open,
                "remember": _remember,
                "recall": _recall,
                "asyncio": _SandboxAsyncio(),
                "json": json,
                "re": re,
            }
        )
        if store is not None:
            namespace.update(_store_bridge(store, clipboard, file_system))

        home_target = getattr(browser_session, "agent_focus_target_id", None)
        code_tab: str | None = None
        if browser_session is not None:
            try:
                code_tab = await _spawn_tab(browser_session, _code_tab_url(fname))
                await _focus_target(browser_session, code_tab)
            except Exception:
                logger.debug("code tab: could not open", exc_info=True)
                code_tab = None
        try:
            result = await _exec_in_sandbox(code, namespace)
        finally:
            if code_tab is not None:
                # @nonobvious(forced-by): refocus BEFORE closing — closing the
                # focused tab fires focus-detach auto-recovery that can race the
                # close and wedge the browser connection; shielded so a step
                # cancellation cannot orphan the tab.
                async def _cleanup() -> None:
                    try:
                        if home_target:
                            await _focus_target(browser_session, home_target)
                        await _close_spawned_tab(browser_session, code_tab)
                    except Exception:
                        logger.warning("code tab: cleanup failed", exc_info=True)

                await asyncio.shield(_cleanup())

        unique_saves = list(dict.fromkeys(saved_files))
        if unique_saves:
            note = "Files saved this run: " + ", ".join(unique_saves) + "."
        else:
            note = (
                "No files were saved by this script — call save_json(obj, 'name.json') "
                "if a later action needs the data."
            )
        if result.error:
            result.error = f"{result.error}\n{note}"[:10000]
        elif result.extracted_content is not None:
            result.extracted_content = f"{result.extracted_content}\n{note}"
        return result


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
    tools: Tools,
    tab_manager: TabManager,
    clipboard: dict[str, Any],
    store: OutputStore | None = None,
    progress: Any = None,
) -> None:
    """Register the multi-tab fan-out actions on a Tools instance. ``store`` lets
    read_pages prefill rows_draft.json; ``progress`` is an async callable streaming
    wave-by-wave read_pages progress to the session feed.
    """

    @tools.action(
        "Read MANY pages in ONE step — the fast way to cover a whole listing. Opens "
        "the URLs in parallel tabs, waits for each to render, reads them, closes "
        "them, and saves the results to pages.json: for each page {url, title, "
        "text, jsonld, links}. Call with NO arguments after find_links: it reads "
        "every found link and automatically reads inside the same embedded panel "
        "your find_links matched (frame_url_contains carries over; pass it only to "
        "override). It ALSO prefills rows_draft.json — one schema row per page, "
        "mapped from the page's jsonld and text — so afterwards you just "
        "add_items_from_file('rows_draft.json'), fix judgement fields with "
        "update_items, and mark_absent what the pages lack; write NO mapping "
        "script. Failed pages are retried once and reported — every listed URL is "
        "covered, so no re-crawling is needed."
    )
    async def read_pages(
        browser_session: BrowserSession,
        file_system: FileSystem,
        urls: list[str] | None = None,
        frame_url_contains: str | None = None,
    ) -> ActionResult:
        try:
            offhost_skipped = 0
            if not urls:
                urls, offhost_skipped = _saved_links_sans_offhost(clipboard)
                if not urls:
                    return ActionResult(
                        error="No urls given and no saved found_links — run find_links first."
                    )
            if frame_url_contains is None:
                frame_url_contains = clipboard.get("found_links_frame")
            dropped = max(0, len(urls) - _READ_PAGES_MAX)
            urls = urls[:_READ_PAGES_MAX]
            pages = await _read_pages_impl(
                browser_session, urls, frame_url_contains, clipboard, progress=progress
            )
            saved: str | None = "pages.json"
            try:
                await file_system.write_file(
                    saved, json.dumps(_pages_for_save(pages), indent=2, default=str)
                )
            except Exception:
                logger.warning("read_pages: failed to save pages.json", exc_info=True)
                saved = None

            draft_note = ""
            if store is not None and store.item_model is not None:
                drafts: list[dict[str, Any]] = []
                thin = 0
                for p in pages:
                    if p.get("error"):
                        continue
                    if len((p.get("text") or "").strip()) < _MIN_PAGE_TEXT_CHARS:
                        thin += 1
                        continue
                    row = _draft_row(store, p)
                    if row:
                        drafts.append(row)
                if drafts:
                    try:
                        await file_system.write_file(
                            "rows_draft.json", json.dumps(drafts, indent=2, default=str)
                        )
                        filled_counts: Counter = Counter()
                        for r in drafts:
                            for k, v in r.items():
                                if v not in (None, "", [], {}):
                                    filled_counts[k] += 1
                        coverage = ", ".join(
                            f"{k} {n}/{len(drafts)}" for k, n in sorted(filled_counts.items())
                        )
                        unfilled = [
                            f
                            for f in store.item_model.model_fields
                            if f not in filled_counts
                        ]
                        draft_note = (
                            f"\nrows_draft.json prefilled with {len(drafts)} row(s) "
                            "mapped from the pages"
                            + (
                                f" ({thin} thin page(s) skipped — probably not records)"
                                if thin
                                else ""
                            )
                            + f". Draft fills: {coverage}."
                            + (
                                " Not in the draft (fill from the listing rows above "
                                "via update_items, or mark_absent): "
                                + ", ".join(unfilled) + "."
                                if unfilled
                                else ""
                            )
                            + " Sample (row #0): "
                            + json.dumps(elide_long_values(drafts[0])[0], default=str)
                            + " Next: add_items_from_file('rows_draft.json'), then ONE "
                            "update_items call for the rest, mark_absent what no page "
                            "publishes, and done. No mapping script is needed."
                        )
                    except Exception:
                        logger.warning("read_pages: failed to save rows_draft.json", exc_info=True)
            ok_count = sum(1 for p in pages if not p.get("error"))
            lines: list[str] = []
            for i, p in enumerate(pages):
                if p.get("error"):
                    lines.append(f"#{i} FAILED {p['url']} — {p['error']}")
                else:
                    listing = " ".join((p.get("listing_text") or "").split())[:80]
                    lines.append(
                        f"#{i} ok {p['url']} — text {len(p.get('text') or '')} chars, "
                        f"jsonld {'yes' if p.get('jsonld') else 'no'}, "
                        f"frame {'yes' if p.get('frame_matched') else 'no'}, "
                        f"{len(p.get('links') or [])} links"
                        + (f" | listing row: {listing}" if listing else "")
                    )
            note = (
                f"Read {ok_count} of {len(pages)} pages"
                + (f"; full content saved to '{saved}'" if saved else "")
                + (
                    f"; skipped {offhost_skipped} off-site link(s) flagged by "
                    "find_links (pass urls explicitly to include them)"
                    if offhost_skipped
                    else ""
                )
                + (
                    f". NOTE: {dropped} URL(s) beyond the {_READ_PAGES_MAX}-page cap "
                    "were NOT read — call read_pages again with the remainder"
                    if dropped
                    else ""
                )
                + ".\n"
                + "\n".join(lines)
                + draft_note
                + (
                    ""
                    if draft_note
                    else "\nNext: ONE run_code_file script that maps "
                    "read_json('pages.json') to schema rows (dates and structured "
                    "details from page['jsonld']), save_json(rows, 'items.json'), "
                    "then add_items_from_file('items.json'). Do not re-read these pages."
                )
            )
            return ActionResult(
                extracted_content=note,
                long_term_memory=f"read_pages: {ok_count}/{len(pages)} pages -> {saved}",
            )
        except Exception as e:
            return ActionResult(error=f"read_pages failed: {type(e).__name__}: {e}")

    @tools.action(
        "Queue URLs as lightweight, UNLOADED background tabs for MANUAL fan-out "
        "(hard cap 48 total). Each becomes a blank about:blank tab at a stable 0-based "
        "index; the real URL is only fetched when you call goto_tab(n). Call with NO "
        "urls to queue every link from your last find_links. Prefer read_pages when "
        "you just need each page's content — it covers the whole listing in one step; "
        "use tabs when you must interact with the pages."
    )
    async def open_tabs(urls: list[str] | None = None) -> ActionResult:
        try:
            if not urls:
                urls, _ = _saved_links_sans_offhost(clipboard)
                if not urls:
                    return ActionResult(
                        error="No urls given and no saved found_links — run find_links first."
                    )
            note = await tab_manager.open_tabs(urls)
            note += (
                " Next: walk them — goto_tab(0), read the detail page, update_item that "
                "item, then goto_tab(1), and so on. Do NOT add items from the listing alone."
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
        "URL matches (e.g. 'embed'); container_index returns only links inside that "
        "element (usually an embed's own index); attr returns links carrying a shared "
        "attribute, e.g. {\"class\": \"posting\"}. Multiple selectors narrow together. "
        "This is the ONLY tool that can read links inside embedded/cross-origin panels. "
        "Lazy-loading is handled for you: the page and any matching panel are "
        "scrolled and settled until the link count is stable, so ONE call collects "
        "the whole listing — do not re-run it to check for late items. "
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
            await _settle_lazy_links(browser_session, frame_url_contains)
            try:
                await browser_session.get_browser_state_summary(include_screenshot=False)
            except Exception:
                logger.debug("find_links: post-settle state refresh failed", exc_info=True)
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

        offhost_count = 0
        if len(links) >= 3:
            hosts = [
                urlparse(link["href"]).netloc.lower().removeprefix("www.")
                for link in links
            ]
            counted = Counter(h for h in hosts if h)
            if counted:
                majority_host = counted.most_common(1)[0][0]
                for link, host in zip(links, hosts):
                    if host and host != majority_host:
                        link["offhost"] = True
                        offhost_count += 1

        clipboard["found_links"] = [link["href"] for link in links]
        clipboard["found_links_offhost"] = {
            link["href"] for link in links if link.get("offhost")
        }
        clipboard["found_links_frame"] = frame_url_contains
        clipboard["found_links_meta"] = {link["href"]: link["text"] for link in links}
        saved: str | None = "found_links.json"
        try:
            await file_system.write_file(saved, json.dumps(links, indent=2))
        except Exception:
            logger.warning("find_links: failed to save found_links.json", exc_info=True)
            saved = None
        frame_hint = ""
        if frame_url_contains:
            frame_hint = (
                f" Reuse the SAME frame_url_contains='{frame_url_contains}' you "
                "matched here; do not look up the iframe's exact src."
            )
        else:
            try:
                embed_hosts = sorted(
                    {
                        urlparse(t.get("url") or "").netloc
                        for t in await _iframe_targets(browser_session)
                        if urlparse(t.get("url") or "").netloc
                    }
                )
            except Exception:
                embed_hosts = []
            if embed_hosts:
                frame_hint = (
                    " Note: this page embeds cross-origin panel(s) "
                    f"({', '.join(embed_hosts)}) that a frameless find_links does "
                    "NOT search — if the listing lives inside one, re-run "
                    "find_links with frame_url_contains matching that host, and "
                    "keep the same frame for read_pages."
                )
        offhost_hint = ""
        if offhost_count:
            offhost_hint = (
                f" {offhost_count} link(s) point at a different site than the rest "
                "(marked offhost — probably navigation/branding); no-args read_pages "
                "skips them automatically, so still call it with no args. Pass "
                "explicit urls only if you DO want an offhost page."
            )
        pointer = (
            f"find_links found {len(links)} link(s), saved as found_links"
            + (f" and {saved}" if saved else "")
            + ". Next: call read_pages() with no args to read them ALL in one step — "
            "each item's detail (description, posted date and more) lives on its own "
            "page, not this listing." + frame_hint + offhost_hint
            + " read_pages prefills rows_draft.json for add_items_from_file — no "
            "mapping script needed. The links stay in view below and via "
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


def _compact_json_text(text: str) -> str | None:
    """Render an oversized JSON tool result with long string values elided as
    ``"<N chars>"`` size markers, keeping the structure whole — honest about what
    was hidden, unlike a head-truncation that silently drops the tail. Tolerates a
    non-JSON prefix/suffix (e.g. a "Read from file x:" header). Returns None when
    the text is not JSON or elision would not shrink it.
    """
    prefix, body, suffix = "", text, ""
    try:
        data = json.loads(text)
    except Exception:
        starts = [i for i in (text.find("{"), text.find("[")) if i >= 0]
        end = max(text.rfind("}"), text.rfind("]"))
        if not starts or end <= min(starts):
            return None
        start = min(starts)
        prefix, body, suffix = text[:start], text[start : end + 1], text[end + 1 :]
        try:
            data = json.loads(body)
        except Exception:
            return None
    compacted, elided = elide_long_values(data)
    if not elided:
        return None
    rendered = json.dumps(compacted, indent=2, default=str)
    return (
        prefix
        + rendered
        + suffix
        + f'\n[{elided} long value(s) elided as "<N chars>" — the underlying data is intact]'
    )


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
                compacted = _compact_json_text(text)
                if compacted is not None and len(compacted) <= 2 * _CAPPED_READ_PREVIEW_CHARS:
                    setattr(
                        result,
                        attr,
                        compacted + f"\n[full data: {total} chars, {tail}] (output #{seen[key]})",
                    )
                    continue
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
    name where to go. Schema-generic and priority-ordered: a source/detail link
    first, then a generic link, and only then a ``*Url`` field that is not a related
    entity's URL (else a ``sellerUrl`` would send the agent to the wrong page ahead
    of the record's real ``sourceUrl``).
    """
    model = store.item_model
    if model is None:
        return None
    names = list(model.model_fields)
    for kw in ("sourceurl", "detailurl", "itemurl", "pageurl", "permalink"):
        for name in names:
            if kw in name.lower():
                return name
    for kw in ("href", "link"):
        for name in names:
            if kw in name.lower():
                return name
    url_fields = [
        n
        for n in names
        if "url" in n.lower()
        and not any(x in n.lower() for x in ("logo", "image", "icon"))
    ]
    # @nonobvious(means): a bare url field is the record's own page; a URL field
    # whose stem prefixes sibling fields (xUrl alongside xName/xDescription) is a
    # related entity's URL, so the first stemmed field WITHOUT such siblings wins.
    for name in url_fields:
        if not _name_tokens(name.lower().replace("url", " ")):
            return name
    for name in url_fields:
        stem_tokens = _name_tokens(name.lower().replace("url", " "))
        if any(other != name and _name_tokens(other) & stem_tokens for other in names):
            continue
        return name
    return None


def _enrichment_note(store: OutputStore, base_msg: str, index: int) -> str:
    """Append to an add_item/update_item result the fields still empty on that item and
    a push to open its own page and fill them — this is what turns a listing stub into
    a full record instead of the finished answer.
    """
    empties = store.item_missing_fields(index)
    if not empties:
        return f"{base_msg} Every field on this item is filled. {store.coverage_summary()}"
    shown = ", ".join(empties[:12])
    if len(empties) > 12:
        shown += f", +{len(empties) - 12} more"
    url_field = _item_url_field(store)
    where = f"its {url_field}" if url_field else "its own page"
    return (
        f"{base_msg} Still empty on this item: {shown}. If you have not read this "
        f"item's own page yet, open {where} and update_item({index}, {{…}}) to fill "
        "what that page shows — detail such as a description or posted date lives on "
        "the item's page, not the listing. A field the site genuinely does not "
        "publish should be settled once with mark_absent, not left blank."
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


def _strip_html(text: str) -> str:
    """Visible text of an HTML fragment: block-level closers become newlines so
    paragraph structure survives, remaining tags drop, entities unescape.
    """
    text = re.sub(r"</(?:p|div|li|ul|ol|h[1-6])>|<br\s*/?>", "\n", text or "", flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_lib.unescape(text)
    text = re.sub(r"[ \t\r\f]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def _extra_style_field(store: OutputStore) -> tuple[str | None, str | None]:
    """The item field meant for unmapped observables, if the schema has one:
    a dict-typed field or a list of {key, value}-shaped objects. Returns
    (field_name, 'dict'|'kv') or (None, None).
    """
    model = store.item_model
    if model is None:
        return None, None
    for name, field in model.model_fields.items():
        inner = _peel_optional(field.annotation)
        origin = get_origin(inner)
        if origin is dict:
            return name, "dict"
        if origin is list:
            args = get_args(inner)
            if args:
                elem = _peel_optional(args[0])
                if isinstance(elem, type) and issubclass(elem, BaseModel):
                    elem_fields = set(elem.model_fields)
                    if "value" in elem_fields and ("key" in elem_fields or "name" in elem_fields):
                        return name, "kv"
    return None, None


_WEAK_TOKENS = {"name", "type", "url", "id", "value", "key", "date", "time", "text", "status"}


def _flatten_jsonld_scalars(
    obj: Any, prefix: str = "", out: dict[str, Any] | None = None, depth: int = 0
) -> dict[str, Any]:
    """Scalar leaves of a JSON-LD object keyed by dotted path, so nested values
    (jobLocation.address.addressLocality) can token-match schema fields the way
    top-level keys do.
    """
    if out is None:
        out = {}
    if depth > 3 or not isinstance(obj, dict):
        return out
    for key, value in obj.items():
        if key.startswith("@"):
            continue
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, (str, int, float, bool)):
            out[path] = value
        elif isinstance(value, dict):
            _flatten_jsonld_scalars(value, path, out, depth + 1)
    return out


def _strong_overlap(a: set[str], b: set[str]) -> bool:
    """True when two token sets share meaning, not just generic vocabulary — at
    least one non-weak token in common, or two weak ones (so 'employmentType'
    cannot claim 'locationType' on the shared 'type' alone).
    """
    common = a & b
    if not common:
        return False
    if common - _WEAK_TOKENS:
        return True
    return len(common) >= 2


def _labelled_pairs(text: str) -> dict[str, str]:
    """Visible label/value pairs from the top of a page's rendered text — a short
    label line followed by a short value line, the way labelled specs render
    ("Condition" / "Brand New"). Pages often show these to the user without
    repeating them in JSON-LD, so the draft must harvest them from the text.
    """
    seq = [ln.strip() for ln in (text or "")[:4000].splitlines() if ln.strip()]
    pairs: dict[str, str] = {}
    for i in range(len(seq) - 1):
        label, value = seq[i], seq[i + 1]
        if not re.fullmatch(r"[A-Za-z][A-Za-z /&-]{1,39}", label):
            continue
        if not value or len(value) > 80:
            continue
        # @nonobvious(forced-by): grouped specs render as "Category" / "Home" /
        # "Home Office" — the fuller next line that extends the value is the real
        # value, the short one is its group prefix.
        if (
            i + 2 < len(seq)
            and seq[i + 2].startswith(value)
            and len(value) < len(seq[i + 2]) <= 80
        ):
            value = seq[i + 2]
        if label.lower() == value.lower():
            continue
        pairs.setdefault(label, value)
    return pairs


def _draft_row(store: OutputStore, page: dict[str, Any]) -> dict[str, Any]:
    """A deterministically prefilled item row from one read page, so the model
    reviews and judges instead of writing parsing code: the page URL fills the
    item's own-URL field, JSON-LD scalars token-match onto schema fields
    (datePublished -> publishedAt), the description lands HTML-stripped, and leftover
    observables go into the extra-style field. Every value is validated against
    its field before inclusion and nothing absent from the page is invented.
    """
    model = store.item_model
    if model is None:
        return {}
    fields = model.model_fields
    row: dict[str, Any] = {}

    def _try_set(fname: str, value: Any) -> bool:
        if fname in row:
            return False
        annotation = fields[fname].annotation
        coerced = _coerce_scalar(value, annotation)
        try:
            TypeAdapter(annotation).validate_python(coerced)
        except Exception:
            return False
        row[fname] = coerced
        return True

    url_field = _item_url_field(store)
    if url_field and page.get("url"):
        _try_set(url_field, page["url"])

    ld = page.get("jsonld") if isinstance(page.get("jsonld"), dict) else {}
    used: set[str] = set()

    desc_candidates = sorted(
        (f for f in fields if "description" in f.lower()), key=len
    )
    desc_field = desc_candidates[0] if desc_candidates else None
    desc = ld.get("description")
    if isinstance(desc, str) and desc.strip():
        used.add("description")
        if desc_field:
            _try_set(desc_field, _strip_html(desc))
    elif desc_field and (page.get("text") or "").strip():
        _try_set(desc_field, (page.get("text") or "")[:20000])

    flat = _flatten_jsonld_scalars(ld)
    for path in sorted(flat, key=lambda p: p.count(".")):
        if path in used or path.split(".", 1)[0] in used:
            continue
        key_tokens = _name_tokens(path.replace(".", " "))
        if not key_tokens:
            continue
        # @nonobvious(forced-by): try every matching field best-first rather than
        # only the single best — a tie like locationType/location is broken by
        # schema order, and if that winner rejects the value (enum vs free text)
        # the runner-up must still get its chance or the value is silently lost.
        candidates = sorted(
            (
                (len(_name_tokens(fname) & key_tokens), fname)
                for fname in fields
                if _strong_overlap(_name_tokens(fname), key_tokens)
            ),
            key=lambda pair: -pair[0],
        )
        for _score, fname in candidates:
            # @nonobvious(forced-by): a page boolean may only fill a boolean field —
            # string coercion would happily store directApply=true as applyUrl="true",
            # poisoning the draft with a value that looks filled but is junk.
            if isinstance(flat[path], bool) and _peel_optional(
                fields[fname].annotation
            ) is not bool:
                continue
            if _try_set(fname, flat[path]):
                used.add(path)
                break

    for label, value in _labelled_pairs(page.get("text") or "").items():
        label_tokens = _name_tokens(label)
        if not label_tokens:
            continue
        label_candidates = sorted(
            (
                (len(_name_tokens(fname) & label_tokens), fname)
                for fname in fields
                if _strong_overlap(_name_tokens(fname), label_tokens)
            ),
            key=lambda pair: -pair[0],
        )
        for _score, fname in label_candidates:
            if _try_set(fname, value):
                break

    title_candidates = [f for f in fields if "title" in f.lower() or f.lower() == "name"]
    if title_candidates and title_candidates[0] not in row and page.get("title"):
        _try_set(title_candidates[0], page["title"])

    for fname in fields:
        if fname in row:
            continue
        low = fname.lower()
        if "url" not in low and "link" not in low:
            continue
        ftokens = _name_tokens(fname) - {"url", "link"}
        if not ftokens:
            continue
        for link in page.get("links") or []:
            if not isinstance(link, dict) or not link.get("href"):
                continue
            link_tokens = _name_tokens(
                f"{link.get('text') or ''} {link.get('href') or ''}".replace("/", " ")
            )
            if ftokens & link_tokens and _try_set(fname, link["href"]):
                break

    extra_field, extra_kind = _extra_style_field(store)
    if extra_field and extra_field not in row:
        leftovers = {
            k: v
            for k, v in ld.items()
            if k not in used
            and not k.startswith("@")
            and isinstance(v, (str, int, float, bool))
        }
        listing_text = page.get("listing_text") or ""
        if listing_text:
            stored_blob = _norm_evidence(json.dumps(row, default=str)).replace(" ", "")
            residue = [
                seg.strip()
                for seg in re.split(r"[•|·\n]+", listing_text)
                if seg.strip()
                and _norm_evidence(seg).replace(" ", "") not in stored_blob
            ]
            if residue:
                leftovers["listing_row"] = " • ".join(residue)[:500]
        if leftovers:
            if extra_kind == "kv":
                for key_name in ("key", "name"):
                    shaped = [
                        {key_name: k, "value": str(v)[:500]} for k, v in leftovers.items()
                    ]
                    if _try_set(extra_field, shaped):
                        break
            else:
                _try_set(extra_field, {k: str(v)[:500] for k, v in leftovers.items()})
    return row


def _load_saved_json(file_system: FileSystem, name: str) -> tuple[Any | None, str]:
    """A saved JSON file's parsed content via the FileSystem registry, falling back
    to disk (the sync save path writes to disk first and the registry catches up a
    beat later). Returns (data, resolved_name); data None when nothing exists.
    """
    fn = _normalise_fs_name(name, "json")
    file_obj = file_system.get_file(fn) or file_system.get_file(name)
    if file_obj is not None:
        return json.loads(file_obj.read()), fn
    path = file_system.get_dir() / fn
    if path.exists():
        return json.loads(path.read_text()), fn
    return None, fn


def _stub_block_msg(
    store: OutputStore, clipboard: dict[str, Any] | None, item: dict[str, Any]
) -> str | None:
    """The refusal message when adding ``item`` would exceed the allowance of
    listing stubs whose own pages have not been opened, else None.
    """
    visited: set = (
        clipboard.setdefault("_visited", set()) if clipboard is not None else set()
    )
    if not _is_bare_stub(store, item, visited):
        return None
    if _bare_stub_count(store, visited) < _MAX_UNVISITED_STUBS:
        return None
    return (
        f"Slow down — you already have {_MAX_UNVISITED_STUBS} listing stubs with no "
        "detail. Read the items' own pages before adding more: read_pages() covers "
        "them all in one step, then add items with the descriptions it returns. Do "
        "not batch items in from the listing."
    )


def _absence_unearned(
    store: OutputStore, clipboard: dict[str, Any] | None, field: str
) -> str | None:
    """The refusal message when ``field`` may not be marked absent yet because the
    items' own pages have not actually been read — absence must be observed, not
    assumed, or gate pressure invites marking everything absent without looking.
    Item fields only; a top-level field has no per-item page to verify against.
    """
    if store.item_model is None or field not in store.item_model.model_fields:
        return None
    url_field = _item_url_field(store)
    if not url_field or not store.array_field:
        return None
    arr = store.data.get(store.array_field) or []
    urls = [
        it.get(url_field)
        for it in arr
        if isinstance(it, dict) and it.get(url_field)
    ]
    if not urls:
        return None
    visited: set = (
        clipboard.setdefault("_visited", set()) if clipboard is not None else set()
    )
    failed: set = (
        clipboard.setdefault("_read_failed", set()) if clipboard is not None else set()
    )
    looked = visited | failed
    unread = [u for u in urls if _norm_url(u) not in looked]
    if unread:
        return (
            f"Cannot mark '{field}' absent yet — {len(unread)} of {len(urls)} item "
            f"pages have not been read (e.g. {unread[0]}). Absence has to be "
            "observed on every item's page: read_pages() covers them all in one "
            "step, then mark it absent if the value is genuinely not there."
        )
    filled = sum(
        1
        for it in arr
        if isinstance(it, dict) and not _is_empty_value_like(it.get(field))
    )
    if filled:
        return (
            f"'{field}' is already settled — it has a value on {filled} item(s) and "
            "every item's page has been read, so the remaining nulls are the honest "
            "final state. A partial field needs no marking; do not mark_absent a "
            "field you found on any page."
        )
    return None


def _is_empty_value_like(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _store_bridge(
    store: OutputStore, clipboard: dict[str, Any], file_system: FileSystem
) -> dict[str, Any]:
    """Sandbox-side handles onto the output store, so a script can write the answer
    directly with the same validation and stub-throttling as the store actions. All
    handles complete synchronously and tolerate await, so a missing ``await`` can
    never silently drop a write.
    """

    def _mirror() -> None:
        _write_fs_file_sync(file_system, "output.json", store.read_output())

    def add_item(item: dict[str, Any]) -> str:
        block = _stub_block_msg(store, clipboard, item)
        if block:
            return _AwaitableStr(block)
        ok, msg = store.add_item(item)
        if ok:
            _mirror()
        return _AwaitableStr(msg)

    def update_item(index: int, fields: dict[str, Any]) -> str:
        ok, msg = store.update_item(index, fields)
        if ok:
            _mirror()
        return _AwaitableStr(msg)

    def update_items(updates: list[dict[str, Any]]) -> str:
        ok, msg = store.update_many(updates)
        if ok:
            _mirror()
        return _AwaitableStr(msg)

    def set_field(key: str, value: Any) -> str:
        ok, msg = store.set_field(key, value)
        if ok:
            _mirror()
        return _AwaitableStr(msg)

    def mark_absent(field: str, reason: str) -> str:
        unearned = _absence_unearned(store, clipboard, field)
        if unearned:
            return _AwaitableStr(unearned)
        _, msg = store.mark_absent(field, reason)
        return _AwaitableStr(msg)

    return {
        "add_item": add_item,
        "update_item": update_item,
        "update_items": update_items,
        "set_field": set_field,
        "mark_absent": mark_absent,
        # @nonobvious(forced-by): read_output must hand scripts a dict, not a JSON
        # string — models write output['items'] and hit "string indices must be
        # integers" when given text, while read_json already returns parsed data.
        "read_output": lambda: _awaitable(json.loads(store.read_output())),
        "coverage": lambda: _AwaitableStr(store.coverage_summary()),
    }


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
    if clipboard is not None:
        store.evidence_check = lambda value: _evidence_contains(
            clipboard.get("_evidence_corpus", ""), value
        )

    @tools.action(
        f"Append one item to the '{array}' list — the primary answer array. "
        f"{_describe_item_fields(store)} Provide every field you already know now; "
        "enrich the rest with update_item once you have read the item's own page. "
        "Validated against the schema and rejected if it does not fit. You may hold at "
        "most two items whose own page you have not read yet — read_pages() covers "
        "them all in one step."
    )
    async def add_item(item: dict[str, Any], file_system: FileSystem) -> ActionResult:
        block = _stub_block_msg(store, clipboard, item)
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
        "visit fills a stub's missing values such as description or publish date. "
        "Re-validated against the schema. To touch several items, use update_items "
        "instead of one call per item."
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
        f"Merge fields into MANY '{array}' items in one step: pass updates as a list "
        'of {"index": n, "fields": {...}} objects. Each merge is schema-validated; '
        "failures are reported per entry without aborting the rest. Always prefer "
        "this over a run of single update_item calls."
    )
    async def update_items(
        updates: list[dict[str, Any]], file_system: FileSystem
    ) -> ActionResult:
        ok, msg = store.update_many(updates)
        if not ok:
            return ActionResult(error=msg)
        await _mirror_output(store, file_system)
        note = f"{msg} {store.coverage_summary()}"
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
        note = f"{msg} {store.coverage_summary()}"
        return ActionResult(extracted_content=note, long_term_memory=note)

    @tools.action(
        "Declare that schema field(s) are genuinely not published on the source "
        "site after you have looked where they should be (a field no detail page "
        "shows anywhere). Pass one field name or a LIST of them (settle them all "
        "in one call) plus a one-line reason saying where you looked. Settled "
        "fields stop counting as unfinished work and done() accepts them empty. A "
        "field found on SOME pages needs no marking — partial is complete once "
        "every page is read."
    )
    async def mark_absent(field: str | list[str], reason: str) -> ActionResult:
        field_names = field if isinstance(field, list) else [field]
        messages: list[str] = []
        errors: list[str] = []
        for name in field_names:
            unearned = _absence_unearned(store, clipboard, name)
            if unearned:
                errors.append(unearned)
                continue
            ok, msg = store.mark_absent(name, reason)
            (messages if ok else errors).append(msg)
        if errors and not messages:
            return ActionResult(error=" ".join(errors))
        note = " ".join(messages)
        if errors:
            note += " NOT settled: " + " ".join(errors)
        note += f" {store.coverage_summary()}"
        return ActionResult(extracted_content=note, long_term_memory=note)

    @tools.action(
        "Read the output you are building so far — a coverage summary plus the "
        "schema with everything you have filled in. Long values render as "
        '"<N chars>" size markers (the stored data is complete); expand one item '
        "in full with index=, or named fields with fields=['name']. On a large "
        "output pass offset=/limit= to window the item array. Every empty field "
        "is either unfinished work or should be mark_absent'ed."
    )
    async def read_output(
        offset: int = 0,
        limit: int | None = None,
        index: int | None = None,
        fields: list[str] | None = None,
    ) -> ActionResult:
        return ActionResult(
            extracted_content=(
                f"{store.coverage_summary()}\n\n"
                f"{store.read_output(offset, limit, compact=True, index=index, fields=fields)}"
            )
        )

    @tools.action(
        "Search the output you have built so far for a case-insensitive substring "
        "across items and fields — use it to check whether you already recorded "
        "something before adding it again."
    )
    async def search_output(query: str) -> ActionResult:
        return ActionResult(extracted_content=store.search_output(query))

    @tools.action(
        f"Bulk-load items into the '{array}' list from a JSON array file you saved "
        "(e.g. save_json(rows, 'items.json') at the end of an extraction script): "
        "validates each element against the schema and appends them in ONE step, "
        "reporting per-index failures. The fast way to fill the output after a script "
        "has read every item's own page. Items whose page you have not opened are "
        "skipped — open them first."
    )
    async def add_items_from_file(name: str, file_system: FileSystem) -> ActionResult:
        try:
            arr, fn = _load_saved_json(file_system, name)
        except Exception as e:
            return ActionResult(error=f"{name} is not valid JSON: {e}")
        if arr is None:
            return ActionResult(
                error=f"No file named {name!r}. Save it first with save_json in a script."
            )
        if not isinstance(arr, list):
            return ActionResult(error=f"{name} must contain a JSON array of items.")

        added = 0
        added_titles: list[str] = []
        failures: dict[str, list[int]] = {}
        blocked = 0
        title_field = next(
            (
                f
                for f in (store.item_model.model_fields if store.item_model else {})
                if "title" in f.lower() or f.lower() == "name"
            ),
            None,
        )
        for i, it in enumerate(arr):
            if not isinstance(it, dict):
                failures.setdefault("not an object", []).append(i)
                continue
            if _stub_block_msg(store, clipboard, it):
                blocked += 1
                continue
            ok, msg = store.add_item(it)
            if ok:
                idx = store.item_count() - 1
                label = str(it.get(title_field) or "")[:40] if title_field else ""
                added_titles.append(f"#{idx} {label}".strip())
                added += 1
            else:
                failures.setdefault(msg, []).append(i)
        await _mirror_output(store, file_system)
        parts = [f"Added {added} of {len(arr)} items from {fn}."]
        if added_titles:
            parts.append("Loaded as: " + "; ".join(added_titles) + ".")
        for reason, indices in failures.items():
            idx = ", ".join(f"#{n}" for n in indices)
            parts.append(f"Rejected {len(indices)} ({idx}): {reason}")
        if blocked:
            parts.append(
                f"{blocked} skipped because their own pages were not read — read_pages() "
                "covers them all in one step before loading."
            )
        if added:
            parts.append(store.coverage_summary())
        note = " ".join(parts)
        return ActionResult(extracted_content=note, long_term_memory=note)

    @tools.action(
        f"Bulk-merge fields into existing '{array}' items from a JSON file you saved: "
        'the file must be an array of {"index": n, "fields": {...}} objects '
        "(e.g. built in a script and saved with save_json). Validates each merge and "
        "reports per-entry failures in ONE step."
    )
    async def update_items_from_file(name: str, file_system: FileSystem) -> ActionResult:
        try:
            arr, fn = _load_saved_json(file_system, name)
        except Exception as e:
            return ActionResult(error=f"{name} is not valid JSON: {e}")
        if arr is None:
            return ActionResult(
                error=f"No file named {name!r}. Save it first with save_json in a script."
            )
        ok, msg = store.update_many(arr)
        if not ok:
            return ActionResult(error=msg)
        await _mirror_output(store, file_system)
        note = f"{msg} {store.coverage_summary()}"
        return ActionResult(extracted_content=note, long_term_memory=note)


def _gate_empty_fields(
    store: OutputStore, clipboard: dict[str, Any] | None
) -> list[str]:
    """The gate's deficiency list, with partially-filled item fields dropped once
    every item's page has been read — per-item absence proven by the read is a
    finished state, and bouncing it pressures the model into marking a field
    absent that it demonstrably found on some pages.
    """
    empties = store.empty_fields()
    if clipboard is None or store.item_model is None or not store.array_field:
        return empties
    arr = store.data.get(store.array_field) or []
    url_field = _item_url_field(store)
    if not arr or not url_field:
        return empties
    urls = [it.get(url_field) for it in arr if isinstance(it, dict) and it.get(url_field)]
    looked = (clipboard.get("_visited") or set()) | (clipboard.get("_read_failed") or set())
    if not urls or any(_norm_url(u) not in looked for u in urls):
        return empties
    filtered: list[str] = []
    for entry in empties:
        m = re.match(r"^(\w+) — empty on (\d+) of (\d+)", entry)
        if m and int(m.group(2)) < int(m.group(3)):
            continue
        filtered.append(entry)
    return filtered


def register_completeness_gate(
    tools: Tools,
    store: OutputStore,
    on_incomplete,
    clipboard: dict[str, Any] | None = None,
) -> None:
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
            empties = _gate_empty_fields(store, clipboard)
            if empties:
                state["bounced"] = True
                if on_incomplete is not None:
                    try:
                        await on_incomplete(empties)
                    except Exception:
                        logger.debug("completeness gate event emit failed", exc_info=True)
                listing = "\n- ".join(empties)
                hints = ""
                try:
                    hint_lines = store.extra_key_hints()
                except Exception:
                    logger.debug("extra_key_hints failed", exc_info=True)
                    hint_lines = []
                if hint_lines:
                    hints = "\n\nShortcuts spotted:\n- " + "\n- ".join(hint_lines)
                date_hint = ""
                if any(
                    any(k in e.lower() for k in ("posted", "published", "date"))
                    for e in empties
                ):
                    date_hint = (
                        "\n\nA posted/published date is usually NOT in the visible text — "
                        "it is in each page's JSON-LD, which read_pages already returns "
                        "as page['jsonld'] (e.g. its 'datePublished')."
                    )
                return ActionResult(
                    is_done=False,
                    extracted_content=(
                        "Not finished — these fields in the output are still empty:\n- "
                        f"{listing}\n\nNo step, time or cost limit has been reached: "
                        "you have ample budget left, so do NOT stop early or claim an "
                        "execution limit. For each field, either fill it (update_items "
                        "in bulk, or go back to the page that shows it) or, if you "
                        "have looked where it should be and the site genuinely does "
                        "not publish it, settle it with mark_absent(field, reason). "
                        "Then call done again." + hints + date_hint
                    ),
                )
        return await original_done(params=params, file_system=file_system)
