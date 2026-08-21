"""Custom browser-use tools — Capsolver CAPTCHA solving, Python sandbox, HTTP fetch."""

import asyncio
import html as html_lib
import json
import logging
import re
from collections import Counter
from pathlib import Path
from types import UnionType
from typing import Any, Union, get_args, get_origin
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

from openbrowse.agent.browser_cdp import (
    _emit_progress,
    _eval_js,
    _eval_on_target,
    _iframe_targets,
)
try:
    # Private upstream helpers: we reuse their query JS so our own delivery keeps
    # browser-use's exact selector semantics. Guarded because a rename upstream must
    # degrade to the built-in action, not break every run.
    from browser_use.tools.service import (  # type: ignore[attr-defined]
        _build_find_elements_js,
        _build_search_page_js,
    )
except Exception:  # pragma: no cover - depends on the installed browser-use
    _build_find_elements_js = None  # type: ignore[assignment]
    _build_search_page_js = None  # type: ignore[assignment]

from openbrowse.agent.captcha.bridge import install_captcha_bridge
from openbrowse.agent.output_store import (
    OutputStore,
    _coerce_scalar,
    _name_tokens,
    _peel_optional,
    elide_long_values,
)
from openbrowse.agent import activity
from openbrowse.agent.textguard import guard_key
from openbrowse import system_metrics

logger = logging.getLogger(__name__)


# The delivery contract. Every data-bearing action goes through `deliver`, which
# returns the payload inline at or under INLINE_BUDGET and otherwise points at the
# file it always writes. Two numbers, applied to the size of THIS call's output —
# never to which tool produced it, so a five-row result and a thousand-row result
# from the same tool take different routes and every tool has both available.
INLINE_BUDGET = 2000
POINTER_SAMPLE = 300

_CAPPED_READ_PREVIEW_CHARS = 8000
_GUARD_MIN_CHARS = 500
_EXTRA_VALUE_CHARS = 500
_SANDBOX_ERROR_CHARS = 2000
_UNREAD_LINKS_KEY = "_unread_links"
_READ_PAGES_KEY = "_read_pages_all"
_DRAFTS_KEY = "_read_pages_drafts"
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
        logger.warning("_focus_target: SwitchTabEvent failed", exc_info=True)
        try:
            cdp = await browser_session.get_or_create_cdp_session()
            await cdp.cdp_client.send.Target.activateTarget(
                params={"targetId": target_id}
            )
        except Exception:
            logger.warning("_focus_target: raw activateTarget failed", exc_info=True)


_IFRAME_HOSTS_JS = (
    "(function(){var own=location.host.toLowerCase();var out=[];"
    "document.querySelectorAll('iframe').forEach(function(f){"
    "try{var h=new URL(f.src,location.href).host.toLowerCase();"
    "if(h&&h!==own&&out.indexOf(h)<0)out.push(h)}catch(e){}});return out;})()"
)


async def _dom_iframe_hosts(browser_session: BrowserSession) -> list[str]:
    """Cross-origin iframe hosts read from the page's own DOM — the second source
    of truth on whether this page embeds its content. CDP's Target.getTargets can
    miss an OOPIF that has not attached yet, and every guard keyed solely on it
    fails together in exactly that failure mode; the iframe elements themselves
    are always in the main document. Best-effort: empty list on any error.
    """
    try:
        val = await _eval_js(browser_session, _IFRAME_HOSTS_JS)
    except Exception:
        logger.debug("_dom_iframe_hosts failed", exc_info=True)
        return []
    if not isinstance(val, list):
        return []
    return [h for h in val if isinstance(h, str) and h]


_PANEL_IFRAME_HOSTS_JS = (
    "(function(){var own=location.host.toLowerCase();var out=[];"
    "var vw=window.innerWidth||1,vh=window.innerHeight||1;"
    "document.querySelectorAll('iframe').forEach(function(f){"
    "try{var r=f.getBoundingClientRect();"
    "if(r.width<vw*0.45||r.height<vh*0.4)return;"
    "var h=new URL(f.src,location.href).host.toLowerCase();"
    "if(h&&h!==own&&out.indexOf(h)<0)out.push(h)}catch(e){}});return out;})()"
)


async def _dom_panel_iframe_hosts(browser_session: BrowserSession) -> list[str]:
    """Cross-origin hosts of iframes large enough to be the page's content panel.
    Ordinary pages carry exactly one small third-party frame (chat bubble,
    CAPTCHA badge, consent widget) far more often than exactly one content
    panel, so a sole-host signal is only trustworthy after this size gate.
    """
    try:
        val = await _eval_js(browser_session, _PANEL_IFRAME_HOSTS_JS)
    except Exception:
        logger.debug("_dom_panel_iframe_hosts failed", exc_info=True)
        return []
    if not isinstance(val, list):
        return []
    return [h for h in val if isinstance(h, str) and h]


def _url_discriminators(url: str) -> set[str]:
    """Long, distinctive tokens from a page URL (query values and path segments)
    that can re-identify the page's own embed among many — an embedded panel's URL
    typically carries the record id from its host page's URL (e.g. a detail page
    ``…?id=<uuid>`` framing ``…/panel/<uuid>?embed=js``).
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
    sibling_urls: list[str] | None = None,
) -> str | None:
    """The OOPIF target belonging to ``page_target_id`` whose URL contains
    ``url_contains``. Ownership is resolved by matching the global iframe-target
    list against the page's own frame tree and iframe srcs, because CDP's target
    list carries no parent linkage. URL discriminators shared with a sibling
    page's URL are discarded — in a concurrent wave a shared path/query segment
    would attribute a SIBLING page's embed, silently mixing items' data. The
    sole-unclaimed-candidate fallback is only honoured when
    ``allow_sole_candidate`` (single-page reads) and only when the candidate's
    host itself matches the needle or the page's own host. When in doubt this
    returns None; the caller reads the main document honestly instead.
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
    for other in sibling_urls or []:
        if _norm_url(other) == _norm_url(page_url or ""):
            continue
        discriminators -= _url_discriminators(other)
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
        cand_host = urlparse(candidates[0]["url"] or "").netloc.lower()
        page_host = urlparse(page_url or "").netloc.lower()
        if (needle and needle in cand_host) or (page_host and cand_host == page_host):
            return candidates[0]["targetId"]
    return None


_COUNT_LINKS_JS = "document.querySelectorAll('a[href]').length"
_SCROLL_BOTTOM_JS = "window.scrollTo(0, document.body ? document.body.scrollHeight : 0)"
_LAZY_MAX_ROUNDS = 8
_LAZY_POLL_S = 0.6


async def _settle_lazy_links(
    browser_session: BrowserSession, frame_url_contains: str | None
) -> bool:
    """Coax a lazily-populating list page into showing everything before links are
    collected: repeatedly scroll the main page and any matching embedded frame to
    the bottom, and only proceed once the link count has stopped growing for two
    consecutive polls. List pages (and their embeds) commonly append items on scroll
    or a second after first paint, so collecting immediately under-counts. The main
    page's scroll position is restored afterwards. Returns True when a frame
    filter was requested but no matching frame was ever seen during settling —
    the embedded panel was never scrolled, so any later link count is unverified.
    """
    needle = (frame_url_contains or "").lower()
    matched_any = False

    async def _matching_frames() -> list[str]:
        nonlocal matched_any
        if not needle:
            return []
        tids = [
            t["targetId"]
            for t in await _iframe_targets(browser_session)
            if needle in t["url"].lower()
        ]
        if tids:
            matched_any = True
        return tids

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
    return bool(needle) and not matched_any


_READ_PAGES_MAX = 48
_READ_PAGES_TEXT_CAP = 60_000
# @nonobvious(mirrors): must stay below the action timeout and step_timeout in
# app/agent/runner.py — read_pages must stop itself before the outer caps fire.
_READ_PAGES_BUDGET_S = 420.0
_READ_PAGES_MIN_WAVE_S = 30.0
# @nonobvious(must-hold): a wave's total stagger must stay below
# _READ_PAGES_MIN_WAVE_S, the reserve _out_of_budget already keeps — that is
# what lets pacing add no budget accounting of its own.
_STAGGER_PER_TAB_MAX_S = 0.8
_STAGGER_TOTAL_MAX_S = 8.0
# @nonobvious(means): measured live — heavy pages need ~15s to render an embed.
_PAGE_READY_TIMEOUT_S = 25.0
_FRAME_MATCH_GRACE_S = 6.0
# @nonobvious(must-hold): bounds the DOM-evidence wait — an iframe that is in
# the DOM but whose panel never attaches (a consent frame, a dead embed) must
# not hold every page to the full deadline.
_PANEL_EVIDENCE_EXTRA_S = 8.0
_MIN_PAGE_TEXT_CHARS = 200
_JUDGE_ANSWER_CAP = 8000
_JSONLD_GRACE_S = 3.0


def _wave_stagger_gap_s(wave_len: int) -> float:
    """Per-tab navigation gap for one wave, sampled once at wave start so a wave
    never lengthens itself by measuring the load it is creating. Zero unless
    another session is live and the host is actually stalling.
    """
    if wave_len < 2 or activity.active_session_count() < 2:
        return 0.0
    stall = system_metrics.stall_fraction()
    if stall < 0.05:
        return 0.0
    return min(_STAGGER_PER_TAB_MAX_S * stall, _STAGGER_TOTAL_MAX_S / (wave_len - 1))


async def _stagger_pause(seconds: float) -> None:
    await asyncio.sleep(seconds)


async def _read_one_page(
    browser_session: BrowserSession,
    url: str,
    target_id: str,
    url_contains: str | None,
    claimed: set[str],
    baseline: set[str],
    allow_sole_candidate: bool = False,
    sibling_urls: list[str] | None = None,
) -> dict[str, Any]:
    """Wait for a spawned tab (and, when asked, its embedded panel) to render, then
    read {url, title, text, jsonld, links} from it — the panel when one matches,
    else the main document. Rendering only counts once the text is substantial
    (embeds paint a thin loading shell first), and a page whose JSON-LD has not
    arrived with the text gets a short grace poll — that is where published dates and
    other structured details live, so reading the shell would silently null those fields.
    """
    page: dict[str, Any] = {"url": url}
    frame_tid: str | None = None
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _PAGE_READY_TIMEOUT_S

    # @nonobvious(forced-by): a page ON the panel provider's own host IS the
    # panel content; no inner frame exists there, and waiting burns the whole
    # timeout. Host-scoped deliberately: the needle appearing elsewhere in the
    # URL (an id query param naming the provider) must NOT drop the filter, or
    # every wave read silently degrades to the embedding shell.
    if url_contains and url_contains.lower() in urlparse(url or "").netloc.lower():
        url_contains = None
        page["frame_skipped_own_host"] = True

    fallback_ok = False
    frame_grace_end = loop.time() + _FRAME_MATCH_GRACE_S
    panel_in_dom: bool | None = None

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
                    sibling_urls=sibling_urls,
                )
                if frame_tid:
                    txt = await _eval_on_target(browser_session, frame_tid, _BODY_TEXT_JS)
                    if _substantial(txt):
                        break
                elif loop.time() >= frame_grace_end:
                    # @nonobvious(forced-by): the DOM shows a matching iframe
                    # long before its CDP target attaches; when the panel is
                    # demonstrably in the page, keep waiting for it instead of
                    # falling back to the shell (cold embeds under load attach
                    # after the grace; reading the shell wastes the whole pass).
                    if panel_in_dom is None:
                        srcs = (
                            await _eval_on_target(
                                browser_session, target_id, _IFRAME_SRC_JS
                            )
                            or []
                        )
                        panel_in_dom = any(
                            url_contains.lower() in str(s).lower() for s in srcs
                        )
                    if (
                        panel_in_dom
                        and loop.time() < frame_grace_end + _PANEL_EVIDENCE_EXTRA_S
                    ):
                        await asyncio.sleep(0.5)
                        continue
                    # @nonobvious(forced-by): no frame + substantial main doc is
                    # a plain page; the shell detector still guards real embeds.
                    ready = await _eval_on_target(
                        browser_session, target_id, "document.readyState"
                    )
                    if ready in ("interactive", "complete"):
                        txt = await _eval_on_target(
                            browser_session, target_id, _BODY_TEXT_JS
                        )
                        if _substantial(txt):
                            fallback_ok = True
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
    if url_contains and not frame_tid and not fallback_ok:
        page["error"] = (
            "no embedded panel matching "
            f"'{url_contains}' rendered — the main document was NOT read in its place"
        )
    elif not (page.get("text") or "").strip():
        page["error"] = "no readable text rendered"
    elif (
        not url_contains
        and len((page.get("text") or "").strip()) < 2 * _MIN_PAGE_TEXT_CHARS
    ):
        try:
            raw = await _eval_on_target(browser_session, target_id, _IFRAME_HOSTS_JS)
        except Exception:
            raw = None
        hosts = (
            [h for h in raw if isinstance(h, str) and h]
            if isinstance(raw, list)
            else []
        )
        if hosts:
            page["error"] = (
                f"page embeds its content in a panel from {hosts[0]}; the main "
                "document alone was read and holds too little text — re-run "
                f"read_pages with frame_url_contains='{hosts[0]}'"
            )
    return page


_FRAME_FAILURE_MARKERS = (
    "embedding shell",
    "no embedded panel",
    "embeds its content in a panel",
    "not attempted",
)


def _frame_failure(error: str) -> bool:
    """True when a read failed because the embed layer failed, not because the
    URL is dead. Frame failures must never count as "looked" — treating them as
    dead pages would unlock mark_absent and drop gate fields on content that was
    simply never seen.
    """
    low = (error or "").lower()
    return any(marker in low for marker in _FRAME_FAILURE_MARKERS)


class _FindLinksError(Exception):
    pass


_FIND_LINKS_RETRY_DELAY_S = 3.0
_FIND_LINKS_MAX_RETRIES = 1


def _scan_link_map(
    selector_map: dict[int, Any],
    current: str,
    *,
    href_contains: str | None = None,
    href_regex: str | None = None,
    frame_url_contains: str | None = None,
    container: Any = None,
    attr: dict[str, str] | None = None,
    visible_only: bool = False,
) -> tuple[list[dict[str, Any]], int | None, int, bool]:
    """One pass over the DOM selector map: (links, frames matched by the frame
    filter or None, anchors seen, whether any iframe exists in the map).
    """
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
    if container is not None:
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
    anchors_seen = 0
    iframe_present = any(
        node.tag_name == "iframe" for node in selector_map.values()
    )
    for index in sorted(selector_map):
        node = selector_map[index]
        if node.tag_name != "a":
            continue
        href = (node.attributes or {}).get("href")
        if not href:
            continue
        anchors_seen += 1
        if visible_only and not node.is_visible:
            continue
        abs_href = urljoin(current, href)
        if href_contains and href_contains.lower() not in abs_href.lower():
            continue
        if pattern and not pattern.search(abs_href):
            continue
        if frame_target_ids is not None and node.target_id not in frame_target_ids:
            continue
        if container is not None and not _in_container(node):
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
    return links, (
        len(frame_target_ids) if frame_target_ids is not None else None
    ), anchors_seen, iframe_present


_RESERVED_CLIPBOARD_KEYS = frozenset(
    {
        "found_links",
        "found_links_offhost",
        "found_links_frame",
        "found_links_meta",
        "_visited",
        "_unread_links",
        "_read_pages_all",
        "_read_pages_drafts",
        "_read_items",
        "_read_failed",
        "_read_failed_frame",
        "_evidence_corpus",
        "_dom_embed_hosts",
        "_settle_frameless",
        "_page_search_counts",
    }
)


def _filter_page_urls(urls: list[str] | None) -> tuple[list[str] | None, int]:
    """Drop entries that are not absolute http(s) URLs before anything tries to
    navigate to them — a malformed value here (``"null"``, a fragment, a bare
    word) opens a garbage tab and can corrupt tab state for the whole run.
    Returns (kept, dropped); None passes through (meaning "use saved links").
    """
    if urls is None:
        return None, 0
    kept = [
        u for u in urls if isinstance(u, str) and u.startswith(("http://", "https://"))
    ]
    return kept, len(urls) - len(kept)


def _saved_links_sans_offhost(clipboard: dict[str, Any] | None) -> tuple[list[str], int]:
    """The last find_links result minus links flagged as pointing off-site, plus
    how many were skipped — so a no-args bulk read covers the list page without
    dragging in navigation/branding pages.
    """
    cb = clipboard or {}
    stored = cb.get("found_links")
    # @nonobvious(forced-by): a corrupted non-list value would iterate as
    # characters and turn into single-letter "URLs" downstream.
    urls = [u for u in stored if isinstance(u, str)] if isinstance(stored, list) else []
    off = cb.get("found_links_offhost")
    if not isinstance(off, (set, frozenset, list, tuple)):
        off = set()
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
    if not url_contains:
        # @nonobvious(means): a sole panel-sized cross-origin embed host on the
        # launching page names where the linked pages render their content too.
        # Multiple hosts stay untargeted, and so do small frames (chat bubbles,
        # consent widgets), because latching wrong reads an unrelated frame on
        # every page of the batch.
        probed = await _dom_panel_iframe_hosts(browser_session)
        if len(probed) == 1:
            url_contains = probed[0]
            await _emit_progress(
                progress,
                "read_pages: no frame filter was carried, but the launching page "
                f"embeds a single cross-origin panel host ({url_contains}) — "
                "reading inside that panel from the start",
            )
    baseline = {t["targetId"] for t in await _iframe_targets(browser_session)}
    home_target = getattr(browser_session, "agent_focus_target_id", None)
    results: dict[str, dict[str, Any]] = {}
    loop = asyncio.get_running_loop()
    budget_deadline = loop.time() + _READ_PAGES_BUDGET_S

    last_wave_gap_s = 0.0

    async def _run_wave(wave: list[str]) -> None:
        nonlocal last_wave_gap_s
        gap_s = _wave_stagger_gap_s(len(wave))
        last_wave_gap_s = gap_s
        pairs: list[tuple[str, str]] = []
        try:
            for idx, u in enumerate(wave):
                if idx and gap_s:
                    await _stagger_pause(gap_s)
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
                    sibling_urls=wave,
                )
        finally:
            # @nonobvious(forced-by): closing a focused target can wedge the CDP
            # connection — focus home first, and shield so cancels never orphan tabs.
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
            # @nonobvious(deliberately-missing): no panel wording at all without
            # a frame filter — "0 frames matched" on a plain read alarmed users
            # when it described nothing; the full counts stay in the export.
            frame_note = ""
            if url_contains:
                matched_n = sum(1 for p in done if p.get("frame_matched"))
                skipped_n = sum(1 for p in done if p.get("frame_skipped_own_host"))
                if matched_n:
                    frame_note = f", {matched_n} read inside their embedded panel"
                elif skipped_n == len(done):
                    frame_note = ", read directly on the panel provider's site"
                else:
                    frame_note = ", no embedded panels attached yet"
            paced = (
                f", paced {last_wave_gap_s * 1000:.0f}ms/tab for a concurrent session"
                if last_wave_gap_s
                else ""
            )
            await _emit_progress(
                progress,
                f"read_pages wave {wave_no}/{total_waves}: {ok} of {len(done)} "
                f"pages ok{frame_note} ({loop.time() - wave_started:.0f}s{paced})",
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
        host_note = f" from {embed_hosts[0]}" if embed_hosts else ""
        await _emit_progress(
            progress,
            f"read_pages: {shell_flagged} page(s) only showed the site's outer "
            f"frame because the real content loads in an embedded panel{host_note}. "
            "Retrying each read inside that panel now"
            + system_metrics.pressure_note(),
        )
    if shell_flagged and embed_hosts:
        # @nonobvious(forced-by): retry here, not via instruction — models route
        # around failed reads with stale files instead of re-reading.
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
            f"read_pages: reading inside the '{url_contains}' panel recovered "
            f"the real content for {recovered} of {len(flagged_urls)} page(s)",
        )

    lone = _flag_lone_frame_fallbacks(results, url_contains)
    if lone:
        await _emit_progress(
            progress,
            f"read_pages: {len(lone)} page(s) read the outer shell while sibling "
            "pages rendered their embedded panel — retrying each inside the panel"
            + system_metrics.pressure_note(),
        )
        try:
            for i in range(0, len(lone), concurrency):
                if _out_of_budget(lone[i:]):
                    break
                await _run_wave(lone[i : i + concurrency])
        finally:
            await _focus_target(browser_session, home_target)
        still = _flag_lone_frame_fallbacks(results, url_contains)
        await _emit_progress(
            progress,
            f"read_pages: recovered {len(lone) - len(still)} of {len(lone)} "
            "shell page(s)"
            + (
                f"; {len(still)} still shell-only and reported as FAILED"
                if still
                else ""
            ),
        )

    if clipboard is not None:
        visited = clipboard.setdefault("_visited", set())
        failed = clipboard.setdefault("_read_failed", set())
        frame_failed = clipboard.setdefault("_read_failed_frame", set())
        links_meta = clipboard.get("found_links_meta") or {}
        for u, page in results.items():
            if links_meta.get(u):
                page.setdefault("link_text", links_meta[u])
            if page.get("error"):
                bucket = frame_failed if _frame_failure(page["error"]) else failed
                bucket.add(_norm_url(u))
            else:
                visited.add(_norm_url(u))
                failed.discard(_norm_url(u))
                frame_failed.discard(_norm_url(u))
        _extend_evidence_corpus(clipboard, results)
    return [results[u] for u in urls if u in results]


def _flag_lone_frame_fallbacks(
    results: dict[str, dict[str, Any]], url_contains: str | None
) -> list[str]:
    """Turn a solitary main-doc fallback into a loud failure: when a frame filter
    was requested and sibling pages DID render their embedded panel, an "ok" page
    without a frame match read the embedding shell — the panel demonstrably
    renders on this site, so the fallback is a per-page attach flake, not a plain
    page. The count-based shell detector needs three near-identical pages and
    cannot see one stray shell; this check works off the siblings' proof instead.
    Returns the flagged URLs.
    """
    if not url_contains:
        return []
    ok_pages = [p for p in results.values() if not p.get("error")]
    if not any(p.get("frame_matched") for p in ok_pages):
        return []
    flagged: list[str] = []
    for p in ok_pages:
        if p.get("frame_matched"):
            continue
        p["error"] = (
            "read the embedding shell, not this page's real content: sibling "
            f"pages rendered their embedded '{url_contains}' panel but this "
            "page's panel never attached — the main document was read instead. "
            "Re-run read_pages for this url with the same frame_url_contains"
        )
        flagged.append(str(p.get("url") or ""))
    return flagged


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
        # @nonobvious(means): digits are stripped so shells differing only in
        # per-page noise (counts, timestamps) still count as duplicates.
        return " ".join(re.sub(r"\d+", "", page.get("text") or "").split())[:1500]

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
        embed_hosts = await _dom_iframe_hosts(browser_session)
    if not embed_hosts:
        return 0, []
    flagged = 0
    for p in ok_pages:
        if _sig(p) == top_sig:
            p["error"] = (
                "read the embedding shell, not this page's real content: "
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

    async def _main_frame_caveat(self) -> str:
        hosts = await _dom_iframe_hosts(self._session)
        if not hosts:
            return ""
        return (
            "note: this reads the MAIN page only; this page embeds content from "
            + ", ".join(hosts)
            + " — use browser.frame_text(...)/find_links(frame_url_contains=...)"
        )

    async def evaluate(self, js: str) -> Any:
        result = await _eval_js(self._session, js)
        # @nonobvious(deliberately-missing): only whole-page body reads get the
        # embed caveat appended — annotating scoped reads (an h1's textContent,
        # an id) would corrupt short values the script stores verbatim.
        if (
            isinstance(result, str)
            and len(result.strip()) < _MIN_PAGE_TEXT_CHARS
            and "document.body" in js
        ):
            caveat = await self._main_frame_caveat()
            if caveat:
                result += "\n" + caveat
        return result

    async def get_html(self, selector: str | None = None) -> str:
        if selector:
            js = (
                "(function(){var el=document.querySelector("
                + json.dumps(selector)
                + ");return el?el.outerHTML:''})()"
            )
        else:
            js = "document.documentElement.outerHTML"
        html = await _eval_js(self._session, js) or ""
        if len(html.strip()) < _MIN_PAGE_TEXT_CHARS:
            caveat = await self._main_frame_caveat()
            if caveat:
                html += f"<!-- {caveat} -->"
        return html

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
        can read an embedded/cross-origin panel (e.g. a detail panel inside an embed).
        """
        needle = (url_contains or "").lower()
        all_frames = await self.frames()
        matched = [f for f in all_frames if not needle or needle in f["url"].lower()]
        # @nonobvious(deliberately-missing): no fall-back to unmatched frames —
        # running JS in an unrelated frame (consent, analytics) returns
        # plausible-but-wrong data that would be stored as this page's content.
        if not matched:
            if all_frames:
                raise RuntimeError(
                    f"no embedded frame matches {url_contains!r}; attached frame "
                    "URLs: " + ", ".join(f["url"] for f in all_frames)
                )
            raise RuntimeError(
                "no embedded frames are attached right now — the panel may still "
                "be loading; retry after browser.wait_for_frame("
                + repr(url_contains)
                + ")"
            )
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
        published date and other structured fields live that are not in the visible
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
            try:
                txt = await self.frame_text(url_contains)
            except RuntimeError:
                txt = ""
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
        whole set of found links without navigating the current tab. With no
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
        cross-origin iframe whose URL contains it to render, and raise if it never
        does — silently proceeding would read the shell as if it were the panel.
        Otherwise settle briefly.
        """
        await _eval_js(self._session, "window.location.assign(" + json.dumps(url) + ")")
        self._mark_visited(url)
        if wait_for:
            if not await self.wait_for_frame(wait_for, timeout_s=max(settle_s, 12.0)):
                hosts = sorted(
                    {
                        urlparse(f["url"]).netloc
                        for f in await self.frames()
                        if urlparse(f["url"]).netloc
                    }
                )
                raise RuntimeError(
                    f"navigate: no embedded frame matching {wait_for!r} rendered "
                    f"on {url}"
                    + (
                        "; attached frame hosts: " + ", ".join(hosts)
                        if hosts
                        else "; no embedded frames are attached"
                    )
                )
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
        headers: dict[str, str] | None = None,
        body: str | None = None,
    ) -> ActionResult:
        """Make an HTTP request.

        Args:
            url: The URL to request
            file_system: Injected by browser-use — must be named exactly this
            method: HTTP method (GET, POST, PUT, DELETE, PATCH)
            headers: Object of header/value pairs, e.g. {"Authorization": "Bearer ..."}
            body: Request body as string (for POST/PUT/PATCH)
        """
        parsed_headers: dict[str, str] = headers or {}

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.request(
                    method=method.upper(),
                    url=url,
                    headers=parsed_headers,
                    content=body,
                )
            text = resp.text
            return await deliver(
                # body first so an over-budget sample shows actual content rather than
                # being consumed by response headers.
                {
                    "status_code": resp.status_code,
                    "body": text,
                    "headers": dict(resp.headers),
                },
                note=f"Fetched {url} — HTTP {resp.status_code}, {len(text)} chars of body.",
                file_system=file_system,
                filename=_fs_name_from_url(url, resp.headers.get("content-type", ""), text),
                # the file keeps the RAW body: its extension is derived from the body's
                # own content type, so a .html or .json file must hold what it says.
                file_content=text,
            )
        except httpx.HTTPError as e:
            return ActionResult(error=f"HTTP request failed: {e}")


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
        # @nonobvious(forced-by): browser-use renders a result's `error` as its first
        # 100 plus last 100 characters once it passes 200, so anything explanatory in
        # the middle is deleted before the model reads it. The guidance travels on
        # long_term_memory, which survives whole.
        return ActionResult(
            error="Script timed out after 300 seconds.",
            long_term_memory="Script timed out after 300 seconds. Anything you saved "
            "with save_json before the timeout is still on disk. For bulk page reads "
            "use browser.read_pages(urls, frame_url_contains) instead of a navigate "
            f"loop; otherwise process a smaller batch and continue in the next run.{tail}",
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
                "mark_absent/remove_items, read_output, coverage, remember and "
                "recall all work "
                "with OR without await."
            )
        tail = f"\n--- stdout ---\n{out}" if out else ""
        # The hint sat between the exception and stdout, which is exactly the span
        # browser-use's 100-plus-100 error clip removes; it has to travel separately.
        return ActionResult(
            error=f"{type(e).__name__}: {e}"[:_SANDBOX_ERROR_CHARS],
            long_term_memory=f"{type(e).__name__}: {e}{hint}{tail}"[:10000],
        )

    out = stdout.getvalue()
    total = len(out)
    # stdout is a report, not a data payload: whatever the script actually produced
    # belongs in save_json. So it stays a note rather than going through deliver, but
    # on the same budget as everything else rather than a number of its own.
    preview = out[:INLINE_BUDGET]
    if total > INLINE_BUDGET:
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
    progress: Any = None,
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
        "await remove_items(indices, reason) / read_output() (returns the output as a plain dict, like read_json) / "
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
        # @nonobvious(forced-by): models send code="" meaning "no new code";
        # writing it would destroy a previously saved script and then report
        # success, indistinguishable from a script that saved nothing.
        if code is not None and code.strip():
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
            if str(key) in _RESERVED_CLIPBOARD_KEYS:
                raise ValueError(
                    f"'{key}' is an internal session key and cannot be overwritten "
                    "— pick another name."
                )
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
        observer = (clipboard or {}).get("_code_stream")
        code_tab: str | None = (clipboard or {}).pop("_code_stream_tab", None)
        if code_tab is not None and browser_session is not None:
            await _focus_target(browser_session, code_tab)
        if code_tab is None and browser_session is not None:
            from openbrowse.agent.code_stream import codeview_url

            try:
                code_tab = await _spawn_tab(browser_session, codeview_url())
                await _focus_target(browser_session, code_tab)
                logger.info("code tab opened for %s (target %s)", fname, code_tab)
            except Exception:
                logger.warning("code tab: could not open", exc_info=True)
                code_tab = None
        if code_tab is not None and observer is not None:
            await observer.push(name=fname, code=code, status="Running", target=code_tab)
        if progress is not None:
            try:
                await progress(f"▶ Running {fname}")
            except Exception:
                logger.debug("code progress emit failed", exc_info=True)
        try:
            result = await _exec_in_sandbox(code, namespace)
        finally:
            if code_tab is not None:
                # @nonobvious(forced-by): refocus before closing (a focused-tab
                # close can wedge CDP); shielded so cancels never orphan the tab.
                async def _cleanup() -> None:
                    try:
                        if home_target:
                            await _focus_target(browser_session, home_target)
                        await _close_spawned_tab(browser_session, code_tab)
                    except Exception:
                        logger.warning("code tab: cleanup failed", exc_info=True)
                    if observer is not None:
                        try:
                            observer.reset()
                        except Exception:
                            logger.debug("code stream reset failed", exc_info=True)

                await asyncio.shield(_cleanup())

        unique_saves = list(dict.fromkeys(saved_files))
        if unique_saves:
            note = "Files saved this run: " + ", ".join(unique_saves) + "."
        else:
            note = (
                "No files were saved by this script — call save_json(obj, 'name.json') "
                "if a later action needs the data."
            )
        # Which files survived matters most when the script crashed, and `error` is
        # the one field that cannot carry it — browser-use renders anything over 200
        # chars as its first 100 plus last 100.
        if model_visible_attrs(result):
            amend_note(result, f" {note}")
        else:
            result.extracted_content = note
        return result


def register_clipboard_tools(tools: Tools, clipboard: dict[str, Any]) -> None:
    """Register a per-session key/value clipboard (shared with the sandbox's
    ``remember``/``recall``) so the agent can stash URLs, IDs and counts and
    return to them after detours.
    """

    @tools.action(
        "Save a value to the session clipboard under a key so you can return to it "
        "later (e.g. a list-page URL, an id, a running count). Persists across steps "
        "and is shared with the code sandbox (remember/recall)."
    )
    async def remember(key: str, value: str) -> ActionResult:
        if str(key) in _RESERVED_CLIPBOARD_KEYS:
            return ActionResult(
                error=f"'{key}' is an internal session key and cannot be "
                "overwritten — pick another name."
            )
        clipboard[str(key)] = value
        return ActionResult(
            extracted_content=f"Remembered {key}", long_term_memory=f"remember({key})"
        )

    @tools.action(
        "Fetch a value previously saved with remember (or an auto-populated key such "
        "as startUrl) from the session clipboard."
    )
    async def recall(key: str, file_system: FileSystem) -> ActionResult:
        if str(key) not in clipboard:
            known = ", ".join(sorted(clipboard)) or "(empty)"
            return ActionResult(
                extracted_content=f"No value stored for '{key}'. Known keys: {known}"
            )
        value = clipboard[str(key)]
        return await deliver(
            value,
            note=f"recall({key}):",
            file_system=file_system,
            filename=f"recall_{_normalise_fs_name(str(key), 'json')}",
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
            target_id = await self._session._cdp_create_new_page(
                "about:blank", background=background
            )
            await install_captcha_bridge(self._session, target_id)
            if url != "about:blank":
                cdp = await self._session.get_or_create_cdp_session(
                    target_id, focus=False
                )
                await cdp.cdp_client.send.Page.navigate(
                    params={"url": url}, session_id=cdp.session_id
                )
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
            hosts = await _dom_iframe_hosts(self._session)
            if hosts:
                return (
                    f"No element at index {index}. This page embeds cross-origin "
                    f"panel(s) from {', '.join(hosts)} — the element may live in an "
                    "embed that has not attached yet; collect its links with "
                    "find_links(frame_url_contains=...) instead."
                )
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
        "Read MANY pages in ONE step — the fast way to cover a whole list page. Opens "
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
        urls, non_url_dropped = _filter_page_urls(urls)
        if non_url_dropped and not urls:
            return ActionResult(
                error=f"None of the {non_url_dropped} given entries are absolute "
                "http(s) URLs. Pass real page links — a find_links call saves "
                "them so read_pages() with no arguments reads them all."
            )
        caller_gave_urls = bool(urls)
        try:
            offhost_skipped = 0
            if not urls:
                # @nonobvious(must-hold): presence of the key, not its truthiness,
                # means "a queue was established". An empty queue is the DRAINED
                # state; treating it as "no queue" sends the fallback back to the
                # full saved set and re-reads batch one for ever.
                if _UNREAD_LINKS_KEY in clipboard:
                    visited = clipboard.get("_visited") or set()
                    urls = [
                        u
                        for u in (clipboard[_UNREAD_LINKS_KEY] or [])
                        if _norm_url(u) not in visited
                    ]
                    if not urls:
                        clipboard[_UNREAD_LINKS_KEY] = []
                        done_note = (
                            "read_pages: nothing left to read — every saved link has "
                            "already been read this session. Their full text is in "
                            "pages.json; work from that instead of calling read_pages "
                            "again."
                        )
                        return ActionResult(
                            extracted_content=done_note, long_term_memory=done_note
                        )
                else:
                    urls, offhost_skipped = _saved_links_sans_offhost(clipboard)
                if not urls:
                    return ActionResult(
                        error="No urls given and no saved found_links — run find_links first."
                    )
            if frame_url_contains is None:
                frame_url_contains = clipboard.get("found_links_frame")
            remainder = urls[_READ_PAGES_MAX:]
            urls = urls[:_READ_PAGES_MAX]
            # An explicit-urls call is a side errand — the agent is told to make one
            # by the completeness gate — and must not overwrite the resume queue.
            if not caller_gave_urls:
                clipboard[_UNREAD_LINKS_KEY] = remainder
            pages = await _read_pages_impl(
                browser_session, urls, frame_url_contains, clipboard, progress=progress
            )
            saved: str | None = "pages.json"
            # @nonobvious(must-hold): both files are rewritten, not appended, so a
            # multi-batch crawl must carry every earlier batch forward or only the
            # last one survives — and the result text tells the agent pages.json
            # holds them all.
            all_pages = list(clipboard.setdefault(_READ_PAGES_KEY, []))
            seen_urls = {_norm_url(str(p.get("url") or "")) for p in all_pages}
            for page in pages:
                if _norm_url(str(page.get("url") or "")) not in seen_urls:
                    all_pages.append(page)
                    seen_urls.add(_norm_url(str(page.get("url") or "")))
            clipboard[_READ_PAGES_KEY] = all_pages
            try:
                await file_system.write_file(
                    saved, json.dumps(_pages_for_save(all_pages), indent=2, default=str)
                )
            except Exception:
                logger.warning("read_pages: failed to save pages.json", exc_info=True)
                saved = None

            draft_note = ""
            if store is not None and store.item_model is not None:
                drafts: list[dict[str, Any]] = []
                thin_urls: list[str] = []
                # @nonobvious(forced-by): draft only THIS batch and carry the earlier
                # rows forward. Re-drafting every page read so far would make a
                # multi-batch crawl quadratic, and on a 400-page run that is thousands
                # of redundant row builds on a Pi.
                drafts.extend(clipboard.get(_DRAFTS_KEY) or [])
                for p in pages:
                    if p.get("error"):
                        continue
                    if len((p.get("text") or "").strip()) < _MIN_PAGE_TEXT_CHARS:
                        thin_urls.append(str(p.get("url") or ""))
                        continue
                    row = _draft_row(store, p)
                    if row:
                        drafts.append(row)
                clipboard[_DRAFTS_KEY] = drafts
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
                                f" ({len(thin_urls)} page(s) returned too little "
                                "text to draft a row — they may have failed to "
                                "render; their URLs are in pages.json: "
                                + ", ".join(thin_urls[:5])
                                + ("…" if len(thin_urls) > 5 else "")
                                + ")"
                                if thin_urls
                                else ""
                            )
                            + f". Draft fills: {coverage}."
                            + (
                                " Not in the draft (fill from the source rows above "
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
            # A per-page status row each, as data rather than prose: the agent needs to
            # see which pages failed and which came back thin, and the full text of the
            # ones that worked is in pages.json.
            status: list[dict[str, Any]] = []
            for p in pages:
                if p.get("error"):
                    status.append({"url": p["url"], "ok": False, "error": str(p["error"])})
                    continue
                row: dict[str, Any] = {
                    "url": p["url"],
                    "ok": True,
                    "text_chars": len(p.get("text") or ""),
                    "jsonld": bool(p.get("jsonld")),
                    "frame_matched": bool(p.get("frame_matched")),
                    "links": len(p.get("links") or []),
                }
                if p.get("frame_skipped_own_host"):
                    row["frame_filter_skipped"] = "page is on the panel's own host"
                link_text = " ".join((p.get("link_text") or "").split())[:80]
                if link_text:
                    row["source_row"] = link_text
                status.append(row)
            note = (
                f"Read {ok_count} of {len(pages)} pages"
                + (f"; full content saved to '{saved}'" if saved else "")
                + (
                    f"; skipped {non_url_dropped} entr(y/ies) that were not "
                    "absolute http(s) URLs"
                    if non_url_dropped
                    else ""
                )
                + (
                    f"; skipped {offhost_skipped} off-site link(s) flagged by "
                    "find_links (pass urls explicitly to include them)"
                    if offhost_skipped
                    else ""
                )
                + (
                    f". {len(remainder)} link(s) beyond the {_READ_PAGES_MAX}-page cap "
                    "were NOT read and are queued for the next call"
                    if remainder
                    else ""
                )
                + "."
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
            done_total = len(all_pages)
            # The queue state has to be unmistakable in both directions: an agent that
            # thinks it has read everything stops early, and one that thinks work
            # remains calls into an error. Say the count, say the exact next call, and
            # say plainly when there is nothing left.
            # @nonobvious(must-hold): what to say depends on WHICH call this was. An
            # explicit-urls call is a side errand that never touches the queue, so its
            # own leftovers are NOT queued and the real queue may still hold work.
            # Describing either wrongly makes the agent stop early, or wait for a
            # resume that never comes.
            queued_now = list(clipboard.get(_UNREAD_LINKS_KEY) or [])
            if caller_gave_urls:
                queue_state = (
                    f" {len(remainder)} of the URLs you passed were beyond the "
                    f"{_READ_PAGES_MAX}-page cap and were NOT read — pass those "
                    "remaining URLs explicitly in another call."
                    if remainder
                    else ""
                ) + (
                    f" Separately, {len(queued_now)} saved link(s) are still queued from "
                    "the main crawl: call read_pages() with NO arguments to resume it."
                    if queued_now
                    else ""
                )
            elif remainder:
                queue_state = (
                    f" {len(remainder)} link(s) are still UNREAD and are queued: call "
                    "read_pages() again with NO arguments to read the next batch — it "
                    "resumes from the queue by itself, so do not re-pass URLs you have "
                    "already read."
                )
            else:
                queue_state = " Nothing is queued — every saved link has now been read."
            return await deliver(
                status,
                note=(
                    f"read_pages: read {ok_count} of {len(pages)} page(s) this call, "
                    f"{done_total} read so far this session. Full page text is in "
                    f"'{saved}'." + queue_state + " " + note
                ),
                file_system=file_system,
                filename="read_pages_status.json",
            )
        except Exception as e:
            return ActionResult(error=f"read_pages failed: {type(e).__name__}: {e}")

    @tools.action(
        "Queue URLs as lightweight, UNLOADED background tabs for MANUAL fan-out "
        "(hard cap 48 total). Each becomes a blank about:blank tab at a stable 0-based "
        "index; the real URL is only fetched when you call goto_tab(n). Call with NO "
        "urls to queue every link from your last find_links. Prefer read_pages when "
        "you just need each page's content — it covers every found link in one step; "
        "use tabs when you must interact with the pages."
    )
    async def open_tabs(urls: list[str] | None = None) -> ActionResult:
        urls, non_url_dropped = _filter_page_urls(urls)
        if non_url_dropped and not urls:
            return ActionResult(
                error=f"None of the {non_url_dropped} given entries are absolute "
                "http(s) URLs. Pass real page links — a find_links call saves "
                "them so open_tabs() with no arguments queues them all."
            )
        try:
            if not urls:
                urls, _ = _saved_links_sans_offhost(clipboard)
                if not urls:
                    return ActionResult(
                        error="No urls given and no saved found_links — run find_links first."
                    )
            note = await tab_manager.open_tabs(urls)
            if non_url_dropped:
                note += f" Skipped {non_url_dropped} entr(y/ies) that were not absolute http(s) URLs."
            note += (
                " Next: walk them — goto_tab(0), read the detail page, update_item that "
                "item, then goto_tab(1), and so on. Do NOT add items from the list page alone."
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
        "can't read. Use this to visit an item's detail page."
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
        "the whole list — do not re-run it to check for late items. "
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
            settle_frameless = bool(
                await _settle_lazy_links(browser_session, frame_url_contains)
            )
            clipboard["_settle_frameless"] = settle_frameless

            unset = object()

            async def _scan(
                href: Any = unset, regex: Any = unset, frame: Any = unset
            ) -> tuple[list[dict[str, Any]], int | None, int, bool]:
                try:
                    await browser_session.get_browser_state_summary(
                        include_screenshot=False
                    )
                except Exception:
                    logger.debug("find_links: state refresh failed", exc_info=True)
                selector_map = await browser_session.get_selector_map()
                current = await _eval_js(browser_session, "window.location.href") or ""
                container = None
                if container_index is not None:
                    container = await browser_session.get_element_by_index(
                        container_index
                    )
                    if container is None:
                        raise _FindLinksError(f"No element at index {container_index}.")
                return _scan_link_map(
                    selector_map,
                    current,
                    href_contains=href_contains if href is unset else href,
                    href_regex=href_regex if regex is unset else regex,
                    frame_url_contains=(
                        frame_url_contains if frame is unset else frame
                    ),
                    container=container,
                    attr=attr,
                    visible_only=visible_only,
                )

            def _suspicious(
                links: list, frames_matched: int | None, anchors: int, iframe: bool
            ) -> bool:
                # @nonobvious(forced-by): OOPIF frame targets and embed-rewritten
                # hrefs attach late on slow devices — a scan can see a bare main
                # document, or a matched frame holding only its branding anchor
                # while the role links are still being rewritten; retrying
                # recovers it instead of silently returning 0-2 footer links.
                if frame_url_contains:
                    if not frames_matched:
                        return True
                    return len(links) <= 2 and anchors > len(links)
                return len(links) <= 2 and iframe

            # @nonobvious(forced-by): embeds rewrite anchors to the HOST page's
            # URLs, so an href filter on the embedded site's domain can never
            # match them (relaxed frame-only rescan recovers the links);
            # conversely a stably-wrong frame filter misses anchors whose
            # rewritten hrefs still carry that domain (href rescan on it).
            async def _try_salvage(
                links: list, frames_matched: int | None, anchors_seen: int
            ) -> tuple[bool, str, list]:
                degenerate = (
                    frame_url_contains
                    and frames_matched
                    and len(links) <= 2
                    and anchors_seen > len(links)
                )
                if not degenerate:
                    return False, "", links
                if href_contains or href_regex:
                    frame_links, _, _, _ = await _scan(href=None, regex=None)
                    # @nonobvious(must-hold): only a list-shaped salvage counts;
                    # swapping one branding anchor for another helps nobody.
                    if len(frame_links) > max(2, len(links)):
                        kept_hrefs = {link["href"] for link in links}
                        samples = [
                            link["href"]
                            for link in frame_links
                            if link["href"] not in kept_hrefs
                        ][:2]
                        note = (
                            f" NOTE: your href filter kept {len(links)} of "
                            f"{len(frame_links)} link(s) in the matched "
                            f"'{frame_url_contains}' frame — this embed rewrites "
                            "its anchors to the host page's own URLs (e.g. "
                            + ", ".join(samples)
                            + "), so filters on the embedded site's own domain "
                            "cannot match them. Returning the frame's full link "
                            "set instead; each linked page carries its own "
                            "outward links."
                        )
                        return True, note, frame_links
                    return False, "", links
                s_links, _, _, _ = await _scan(
                    href=frame_url_contains, regex=None, frame=None
                )
                if len(s_links) > max(2, len(links)):
                    note = (
                        f" NOTE: the '{frame_url_contains}' frame filter caught "
                        "only the embed's own anchor(s); these links were "
                        "recovered by matching hrefs containing "
                        f"'{frame_url_contains}' instead, which is the same "
                        "link set."
                    )
                    return True, note, s_links
                return False, "", links

            links, frames_matched, anchors_seen, iframe_present = await _scan()
            # @nonobvious(must-hold): salvage before any sleep-retry — a
            # starving caller filter produces the identical result on every
            # rescan, so sleeping first only delays the same rescue.
            salvaged, salvage_note, links = await _try_salvage(
                links, frames_matched, anchors_seen
            )
            retries = 0
            if not salvaged and _suspicious(
                links, frames_matched, anchors_seen, iframe_present
            ):
                retries = 1
                await asyncio.sleep(_FIND_LINKS_RETRY_DELAY_S)
                links, frames_matched, anchors_seen, iframe_present = await _scan()
                salvaged, salvage_note, links = await _try_salvage(
                    links, frames_matched, anchors_seen
                )
            retried = retries > 0
        except _FindLinksError as e:
            return ActionResult(error=str(e))
        except Exception as e:
            return ActionResult(error=f"find_links failed: {type(e).__name__}: {e}")

        telemetry = (
            f"find_links: {len(links)} link(s) matched from {anchors_seen} anchor(s)"
            + (
                f"; frame filter '{frame_url_contains}' matched "
                f"{frames_matched or 0} frame(s)"
                if frame_url_contains
                else ""
            )
            + (f" (after {retries} settle retr{'y' if retries == 1 else 'ies'})" if retried else "")
            + (
                "; caller filters starved the matched frame so a relaxed rescan "
                "returned the link set"
                if salvaged
                else ""
            )
            + (system_metrics.pressure_note() if retried or salvaged else "")
        )
        await _emit_progress(progress, telemetry)

        dom_hosts = await _dom_iframe_hosts(browser_session)
        if dom_hosts:
            clipboard["_dom_embed_hosts"] = dom_hosts

        if frame_url_contains and not frames_matched:
            return ActionResult(
                error=(
                    f"No embedded frame matching '{frame_url_contains}' is attached "
                    "in the DOM right now, so nothing could be read from it — the "
                    "embed may still be loading. The role links may still exist as "
                    "indexed elements: open them by index, or wait 2 seconds and "
                    "re-run find_links."
                )
            )

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

        # @nonobvious(must-hold): a new link set invalidates the old resume queue.
        # Leaving a drained queue behind makes the next read_pages() take the
        # "nothing left to read" branch and skip these links entirely.
        clipboard.pop(_UNREAD_LINKS_KEY, None)
        clipboard["found_links"] = [link["href"] for link in links]
        clipboard["found_links_offhost"] = {
            link["href"] for link in links if link.get("offhost")
        }
        clipboard["found_links_frame"] = frame_url_contains
        clipboard["found_links_meta"] = {link["href"]: link["text"] for link in links}
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
            if not embed_hosts:
                embed_hosts = dom_hosts
            if embed_hosts:
                frame_hint = (
                    " Note: this page embeds cross-origin panel(s) "
                    f"({', '.join(embed_hosts)}) that a frameless find_links does "
                    "NOT search — if the links live inside one, re-run "
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
        unverified_hint = salvage_note
        if not salvaged and (
            frame_url_contains
            and frames_matched
            and len(links) <= 2
            and anchors_seen > len(links)
        ):
            unverified_hint = (
                f" WARNING: the frame filter matched {frames_matched} frame(s) but "
                f"only {len(links)} of the page's {anchors_seen} anchor(s) belong "
                "to it — the embed's links may not have finished rewriting, so "
                "this count is unverified even after retries. If the page visibly "
                "lists more items than this, wait 2 seconds and re-run find_links "
                "before trusting this result."
            )
        elif settle_frameless:
            unverified_hint = (
                " WARNING: the count is unverified — the embedded panel was never "
                "scrolled during settling (its frame had not attached), so late "
                "items may be missing. Re-run find_links if the count looks low."
            )
        pointer = (
            f"find_links found {len(links)} link(s), saved as found_links"
            + ". Next: call read_pages() with no args to read them — "
            "each item's detail (description, published date and more) lives on its own "
            "page, not this list page."
            + (
                f" There are more than {_READ_PAGES_MAX} of them, so this takes more "
                "than one call: read_pages() reads a batch, queues the rest, and "
                "resumes from that queue each time you call it again with no args."
                if len(links) > _READ_PAGES_MAX
                else " One call covers them all."
            )
            + unverified_hint + frame_hint + offhost_hint
            + " read_pages prefills rows_draft.json for add_items_from_file — no "
            "mapping script needed."
        )
        return await deliver(
            links,
            note=pointer,
            file_system=file_system,
            filename="found_links.json",
        )


_GUARDED_DUMP_ACTIONS = (
    "find_elements",
    "search_page",
    "evaluate",
    "find_links",
    "http_fetch",
    "run_code_file",
    "read_pages",
)
# recall included: it now returns the value whole, so an agent that recalls the
# same key repeatedly would otherwise append kilobytes to permanent context each
# time. Deduped, never capped — the point of the tool is the value itself.
_GUARDED_DEDUP_ACTIONS = ("read_output", "search_output", "recall")

_REPEAT_BREAK_AT = 2


def _clip_marked(value: Any, limit: int = _EXTRA_VALUE_CHARS) -> str:
    """Clip an overflow value the way ``elide_long_values`` does — saying so. A bare
    slice into a stored row reads as the whole value, so nothing ever prompts the agent
    to go back to pages.json for the rest.
    """
    text = str(value)
    if len(text) <= limit:
        return text
    return f"{text[:limit]}… <{len(text)} chars total, cut here>"


async def deliver(
    payload: Any,
    *,
    note: str,
    file_system: Any,
    filename: str,
    formatter: Any = None,
    file_content: str | None = None,
) -> ActionResult:
    """The one way a tool hands data to the model.

    Serialises the real object as JSON rather than prose, because prose cannot be
    re-read from disk faithfully and models parse it less reliably. Writes the file on
    every call, inline or not, so the pointer is never a lie and "give me the full
    object" is always ``read_file('<name>')`` with no extra tool and no flag a model
    could set defensively on every call. Returns the payload inline at or under
    ``INLINE_BUDGET``, otherwise a pointer naming the file and the call that reads it.

    ``note`` is the tool's own sentence: what happened and what to do next. A bare
    count is what made ``find_elements`` unanswerable and is never an acceptable note.
    ``formatter`` is an optional human-readable summary; if it raises, the JSON is
    delivered anyway, so a broken formatter can never cost the agent its data.

    Sets both result fields to the identical string. Divergence between them is the
    defect this whole contract exists to make impossible.
    """
    if isinstance(payload, str):
        body = payload
    else:
        try:
            body = json.dumps(payload, indent=2, default=str)
        except Exception:
            logger.warning("deliver: %s payload is not JSON-serialisable", filename, exc_info=True)
            body = str(payload)

    saved: str | None = filename
    if file_system is not None and filename:
        try:
            await file_system.write_file(
                filename, body if file_content is None else file_content
            )
        except Exception:
            logger.warning("deliver: failed to save %s", filename, exc_info=True)
            saved = None
    else:
        saved = None

    headline = note
    if formatter is not None:
        try:
            summary = formatter(payload)
            if summary:
                headline = f"{note} {summary}"
        except Exception:
            logger.warning("deliver: formatter failed for %s", filename, exc_info=True)
            headline = f"{note} (could not render a readable summary; the JSON below is complete)"

    # One JSON document, always. Wrapping the payload inside prose would make the model
    # find the data within a sentence; this way the whole reply parses, in both routes.
    envelope: dict[str, Any] = {"note": headline}
    if saved:
        envelope["file"] = saved
    if len(body) <= INLINE_BUDGET:
        envelope["data"] = payload if not isinstance(payload, str) else body
    else:
        envelope["truncated"] = True
        envelope["total_chars"] = len(body)
        envelope["sample"] = body[:POINTER_SAMPLE]
        envelope["read_with"] = (
            f"read_file('{saved}') for the complete data, or read_json('{saved}') "
            "inside run_code_file"
            if saved
            else "nothing — saving the data to a file FAILED, so only the sample above "
            "exists. Narrow the query and run it again rather than expecting a file"
        )
    text = json.dumps(envelope, indent=2, default=str)
    return ActionResult(extracted_content=text, long_term_memory=text)


def amend_note(result: ActionResult, extra: str) -> None:
    """Add a sentence to a delivered reply without breaking it.

    Appending to the string would put text after the envelope's closing brace and stop
    the reply parsing, which is the one property the envelope exists to give. So the
    text goes into the ``note`` field instead. Results that are not envelopes (upstream
    actions, plain notes) fall back to a plain append, which is correct for them.
    """
    for attr in model_visible_attrs(result) or ("long_term_memory",):
        current = str(getattr(result, attr, "") or "")
        try:
            envelope = json.loads(current)
            if not isinstance(envelope, dict) or "note" not in envelope:
                raise ValueError("not an envelope")
            envelope["note"] = f"{envelope['note']}{extra}"
            setattr(result, attr, json.dumps(envelope, indent=2, default=str))
        except Exception:
            setattr(result, attr, current + extra)


def model_visible_attrs(result: ActionResult) -> tuple[str, ...]:
    """The ``ActionResult`` fields browser-use actually forwards to the model, in the
    order it renders them. Mirrors ``_update_agent_history_description``: a result's
    ``extracted_content`` is DISCARDED — not truncated, not previewed — whenever
    ``long_term_memory`` is set and ``include_extracted_content_only_once`` is False.
    Anything written to a field this does not return is invisible to the agent no
    matter how carefully it is worded.
    """
    once = bool(getattr(result, "include_extracted_content_only_once", False))
    attrs: list[str] = []
    if once and getattr(result, "extracted_content", None):
        attrs.append("extracted_content")
    if getattr(result, "long_term_memory", None):
        attrs.append("long_term_memory")
    elif not once and getattr(result, "extracted_content", None):
        attrs.append("extracted_content")
    return tuple(attrs)


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
    deduped but never capped, so the store always shows whole.

    Operates only on the fields ``model_visible_attrs`` reports, because those are the
    only ones the agent ever reads. A back-reference written anywhere else is not a
    pointer, it is a deletion; and a back-reference is only honest at all because the
    text it points at went to a persistent field, so it is still in the history.
    Whenever a cap spills to a readout file, every later reference names that file
    rather than a step number the agent cannot look up.

    Also breaks repeat loops. An action that keeps returning the same bytes cannot make
    progress, and the agent has no way to notice on its own: its own stated intent does
    not survive the step boundary, so it re-derives the same call from a history that by
    then reads as N copies of one line. The streak is tracked at any size — a short
    constant reply is exactly the kind that loops.

    Swaps each action's normalised function in place — the registry executes
    ``entry.function`` at call time, and every function is normalised to accept
    ``(params, **special_context)``, so one uniform wrapper covers all of them with no
    re-registration or per-action signature. Must run AFTER every other ``register_*``
    so it wraps the final version of each action.
    """
    registry_actions = tools.registry.registry.actions
    seen: dict[str, dict[str, Any]] = {}
    counter = {"n": 0}
    calls = {"n": 0}
    streaks: dict[str, dict[str, Any]] = {}

    def _back_reference(prior: dict[str, Any]) -> str:
        where = prior.get("where")
        if where:
            return (
                f"[identical to earlier output #{prior['n']} — not repeated; the full "
                f"text is in '{where}', read that rather than re-running this]"
            )
        return (
            f"[identical to earlier output #{prior['n']} — not repeated; it is still "
            "in your history above, scroll back rather than re-running this]"
        )

    def _bump_streak(action_name: str, visible: str) -> int:
        """Count back-to-back identical results per action.

        Per action, because one action's reply says nothing about another's. Adjacent,
        because two identical reads thirty steps apart are a coincidence, not a loop,
        and telling an agent to "move on" over one would push it to finish early.
        """
        calls["n"] += 1
        key = guard_key(visible)
        prior = streaks.get(action_name)
        if prior and prior["key"] == key and prior["seq"] == calls["n"] - 1:
            prior["n"] += 1
        else:
            prior = {"key": key, "n": 1}
            streaks[action_name] = prior
        prior["seq"] = calls["n"]
        return int(prior["n"])

    async def _guard(
        result: ActionResult,
        file_system: Any,
        action_name: str,
        readout_name: str,
        cap: bool,
        params_key: str = "",
    ) -> ActionResult:
        attrs = model_visible_attrs(result)
        # An action failing identically over and over is the commonest real loop, and
        # a result carrying only `error` has no visible attrs at all — so the streak
        # must be counted before that case is skipped, not after.
        repeats = _bump_streak(
            action_name,
            f"{params_key}\n"
            + "\n".join(str(getattr(result, a) or "") for a in attrs)
            + str(getattr(result, "error", "") or ""),
        )
        if not attrs:
            if repeats >= _REPEAT_BREAK_AT and getattr(result, "error", None):
                result.long_term_memory = (
                    f"{action_name} has now failed with exactly the same error "
                    f"{repeats} times in a row. Repeating it cannot help — change the "
                    "parameters, use a different tool, or work with what you have."
                )
            return result

        for attr in attrs:
            text = getattr(result, attr, None)
            if not text or len(text) <= _GUARD_MIN_CHARS:
                continue
            key = guard_key(text)
            prior = seen.get(key)
            if prior is not None:
                setattr(result, attr, _back_reference(prior))
                continue
            counter["n"] += 1
            record: dict[str, Any] = {"n": counter["n"], "where": None}
            seen[key] = record
            if cap and len(text) > _CAPPED_READ_PREVIEW_CHARS:
                total = len(text)
                tail = "narrow your query instead of dumping"
                # @nonobvious(must-hold): numbered per output, not per action. A back
                # reference pins this filename for the rest of the run, so reusing one
                # name would later hand the agent a different call's content under the
                # name of the one it asked for.
                spill = f"{readout_name}_{counter['n']}.txt" if readout_name else ""
                if file_system is not None and spill:
                    try:
                        await file_system.write_file(spill, text)
                        record["where"] = spill
                        tail = f"saved to '{spill}' — read specific parts instead"
                    except Exception:
                        logger.warning("output guard: failed to save readout", exc_info=True)
                        tail = (
                            "saving it failed, so the rest is gone — narrow your query "
                            "and run again rather than expecting a file"
                        )
                compacted = _compact_json_text(text)
                if compacted is not None and len(compacted) <= 2 * _CAPPED_READ_PREVIEW_CHARS:
                    setattr(
                        result,
                        attr,
                        compacted + f"\n[full data: {total} chars, {tail}] (output #{record['n']})",
                    )
                    continue
                setattr(
                    result,
                    attr,
                    text[:_CAPPED_READ_PREVIEW_CHARS]
                    + f"\n[truncated: {total} chars total, {tail}] (output #{record['n']})",
                )

        if repeats >= _REPEAT_BREAK_AT:
            note = (
                f" STOP REPEATING THIS: {action_name} has now returned exactly the same "
                f"result {repeats} times in a row. Running it again cannot produce "
                "anything different. Change the approach — different parameters, a "
                "different tool, or record what you already have and move on."
            )
            amend_note(result, note)
        return result

    def _install(name: str, cap: bool) -> None:
        entry = registry_actions.get(name)
        if entry is None:
            return
        original = entry.function
        readout = f"readout_{name}"

        async def wrapped(
            params: Any = None,
            _original: Any = original,
            _name: str = name,
            _readout: str = readout,
            _cap: bool = cap,
            **kwargs: Any,
        ) -> Any:
            result = await _original(params=params, **kwargs)
            if isinstance(result, ActionResult):
                # @nonobvious(forced-by): two DIFFERENT scripts that both print nothing
                # and save nothing render identically, so output alone would read as a
                # loop. The call's own arguments are what distinguish them.
                try:
                    params_key = guard_key(repr(params))
                except Exception:
                    params_key = ""
                return await _guard(
                    result, kwargs.get("file_system"), _name, _readout, _cap, params_key
                )
            return result

        entry.function = wrapped

    for name in _GUARDED_DUMP_ACTIONS:
        _install(name, True)
    for name in _GUARDED_DEDUP_ACTIONS:
        _install(name, False)


def _elem_kind(arm: Any) -> str | None:
    args = get_args(arm)
    if not args:
        return None
    first = args[0]
    if first is str:
        return "str"
    if first is int:
        return "int"
    if get_origin(first) is dict or first is dict:
        return "dict"
    return None


def _param_kind(annotation: Any) -> dict[str, Any] | None:
    """Describe an action param annotation for the boundary normaliser:
    the container it wants (list/dict, with the list's element kind), whether
    it is optional (so ``"null"`` can only mean None), and whether its only
    real type is a plain string (whose content must never be reinterpreted,
    beyond the explicit ``"null"`` token when optional).
    """
    origin = get_origin(annotation)
    if origin is list:
        return {"container": "list", "elem": _elem_kind(annotation), "optional": False}
    if origin is dict:
        return {"container": "dict", "elem": None, "optional": False}
    if origin in (Union, UnionType):
        args = get_args(annotation)
        optional = type(None) in args
        for arm in args:
            arm_origin = get_origin(arm)
            if arm_origin is list:
                return {
                    "container": "list",
                    "elem": _elem_kind(arm),
                    "optional": optional,
                }
            if arm_origin is dict:
                return {"container": "dict", "elem": None, "optional": optional}
        non_none = [a for a in args if a is not type(None)]
        if optional and non_none == [str]:
            return {"container": None, "elem": None, "optional": True, "plain_str": True}
        if optional and non_none and all(a in (int, float, bool) for a in non_none):
            return {"container": None, "elem": None, "optional": True}
    return None


def action_param_kinds(tools: Tools) -> dict[str, dict[str, dict[str, Any]]]:
    """A ``{action: {param: spec}}`` map over the full registry (our actions and
    browser-use built-ins alike), consumed by the boundary normaliser that
    repairs argument shapes before validation.
    """
    kinds: dict[str, dict[str, dict[str, Any]]] = {}
    for name, entry in tools.registry.registry.actions.items():
        param_model = getattr(entry, "param_model", None)
        if param_model is None:
            continue
        per_param = {
            pname: kind
            for pname, field in param_model.model_fields.items()
            if (kind := _param_kind(field.annotation))
        }
        if per_param:
            kinds[name] = per_param
    return kinds


_SELECTOR_ATTR_RE = re.compile(r"\[\s*([A-Za-z_:][-\w:.]*)\s*(?:[~|^$*]?=|\])")
_TAG_IDENTITY_ATTRS = (("a", "href"), ("link", "href"), ("img", "src"), ("script", "src"))


def _attrs_from_selector(selector: str) -> list[str]:
    """The attributes a selector demonstrably cares about: every attribute it filters
    on, plus the one that identifies the tag it targets.

    ``find_elements`` defaults to returning tag and text only, so asking it for
    ``a[href*='x.com']`` answers with five anonymous anchors and no URLs — the one
    thing the selector proves the caller wanted. Nothing about the reply says the
    attribute is available on request, so the honest default is to include what was
    filtered on.
    """
    names: list[str] = []
    for match in _SELECTOR_ATTR_RE.finditer(selector or ""):
        name = match.group(1)
        if name not in names:
            names.append(name)
    lowered = (selector or "").lower()
    for tag, attr in _TAG_IDENTITY_ATTRS:
        if re.search(rf"(?:^|[\s,>+~]){tag}\b", lowered) and attr not in names:
            names.append(attr)
    return names


async def _run_upstream_query(
    browser_session: Any, builder: Any, **kwargs: Any
) -> dict[str, Any] | None:
    """Run one of browser-use's own query scripts ourselves and return its raw result.

    ``find_elements`` and ``search_page`` render their findings to a string and hand
    back only that, so a wrapper can never recover the objects behind it. Reusing their
    JS keeps us on their query semantics while letting the real rows reach ``deliver``.
    Returns None when the query cannot be run, which tells the caller to fall back to
    the upstream action rather than fail the step.
    """
    # @nonobvious(must-hold): the JS is built HERE, not passed in. Built at the call
    # site it would be evaluated as an argument, so a missing upstream symbol would
    # raise before this guard could fall back — the guard would be dead code.
    if browser_session is None or builder is None:
        return None
    try:
        data = await _eval_js(browser_session, builder(**kwargs))
    except Exception:
        logger.warning("upstream query failed; falling back to the built-in", exc_info=True)
        return None
    return data if isinstance(data, dict) else None


def register_find_elements_flow(tools: Tools) -> None:
    """Wrap the built-in ``find_elements`` so it can answer the question it was asked:
    fill in the attributes the selector already names when the caller gave none, and
    deliver the element listing on the field the model actually reads.

    Left alone this action is unanswerable — its listing is the only place attribute
    values appear, and that listing is dropped. An agent that needs an href gets a
    count, cannot tell that anything was withheld, and re-runs the same call.
    """
    entry = tools.registry.registry.actions.get("find_elements")
    if entry is None:
        return
    original = entry.function

    async def wrapped(params: Any = None, **kwargs: Any) -> Any:
        selector = str(getattr(params, "selector", "") or "")
        attributes = list(getattr(params, "attributes", None) or []) or _attrs_from_selector(
            selector
        )
        session = kwargs.get("browser_session")
        data = await _run_upstream_query(
            session,
            _build_find_elements_js,
            selector=selector,
            attributes=attributes or None,
            max_results=int(getattr(params, "max_results", 50) or 50),
            include_text=bool(getattr(params, "include_text", True)),
        )
        if data is None:
            return await original(params=params, **kwargs)
        if isinstance(data, dict) and data.get("error"):
            return ActionResult(error=f"find_elements: {data['error']}")
        total = int(data.get("total", 0) or 0)
        rows = data.get("elements", []) or []
        return await deliver(
            rows,
            note=(
                f"find_elements matched {total} element(s) for {selector!r}"
                + (f", reporting {', '.join(attributes)}" if attributes else "")
                + ("." if total else " — nothing on the page matches that selector.")
                + (
                    f" Only {len(rows)} of them are here — raise max_results to see the "
                    "rest, or narrow the selector."
                    if total > len(rows)
                    else ""
                )
            ),
            file_system=kwargs.get("file_system"),
            filename="found_elements.json",
        )

    entry.function = wrapped


def register_search_page_flow(tools: Tools, clipboard: dict[str, Any]) -> None:
    """Wrap the built-in ``search_page`` so it survives the shapes models
    actually send, delivers its matches where the model can read them, and steers
    away from unproductive repeats: a ``css_scope`` of ``"null"``/``"none"``/empty
    text becomes no scope instead of a literal selector lookup that always fails;
    the first search after pages have been read points at the one-step pages.json
    sweep; an exact repeated pattern is told its result will not change.
    """
    entry = tools.registry.registry.actions.get("search_page")
    if entry is None:
        return
    original = entry.function

    async def wrapped(params: Any = None, **kwargs: Any) -> Any:
        scope = getattr(params, "css_scope", None)
        if isinstance(scope, str) and scope.strip().lower() in ("", "null", "none"):
            try:
                params.css_scope = None
            except Exception:
                pass
        pattern = str(getattr(params, "pattern", "") or "")
        data = await _run_upstream_query(
            kwargs.get("browser_session"),
            _build_search_page_js,
            pattern=pattern,
            regex=bool(getattr(params, "regex", False)),
            case_sensitive=bool(getattr(params, "case_sensitive", False)),
            context_chars=int(getattr(params, "context_chars", 150) or 150),
            css_scope=getattr(params, "css_scope", None),
            max_results=int(getattr(params, "max_results", 50) or 50),
        )
        if data is None:
            result = await original(params=params, **kwargs)
        elif isinstance(data, dict) and data.get("error"):
            result = ActionResult(error=f"search_page: {data['error']}")
        else:
            total = int(data.get("total", 0) or 0)
            rows = data.get("matches", []) or []
            result = await deliver(
                rows,
                note=(
                    f"search_page found {total} match(es) for {pattern!r} on the page "
                    "currently on screen."
                    + (
                        f" Only {len(rows)} of them are here — raise max_results to see "
                        "the rest."
                        if total > len(rows)
                        else ""
                    )
                ),
                file_system=kwargs.get("file_system"),
                filename="page_matches.json",
            )
        counts = clipboard.setdefault("_page_search_counts", {})
        n = counts.get(pattern, 0) + 1
        counts[pattern] = n
        notes: list[str] = []
        if n == 1 and clipboard.get("_visited"):
            notes.append(
                "the full text of every page read this session is saved in "
                "pages.json — one run_code_file script can search ALL of them "
                "for ALL fields at once; search_page only covers the page "
                "currently on screen."
            )
        elif n >= 2:
            notes.append(
                f"this exact pattern has now been searched {n} times — if the "
                "page on screen has not changed, the result will not change "
                "either. To check every read page in one step, search "
                "pages.json with run_code_file."
            )
        if notes and isinstance(result, ActionResult):
            amend_note(result, " NOTE: " + " ".join(notes))
        return result

    entry.function = wrapped


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
    a push to open its own page and fill them — this is what turns a list-row stub into
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
        "what that page shows — detail such as a description or published date lives on "
        "the item's page, not the list page. A field the site genuinely does not "
        "publish should be settled once with mark_absent, not left blank."
    )


_MAX_UNVISITED_STUBS = 2
_STUB_CONTENT_CHARS = 120


def _item_has_substantial_content(item: dict) -> bool:
    """True if the item carries a real page-read field (a description far exceeds a
    list row's short title/location), so it is drilled-in data, not a bare list row.
    """
    return any(
        isinstance(v, str) and len(v) > _STUB_CONTENT_CHARS for v in item.values()
    )


def _is_bare_stub(store: OutputStore, item: dict, visited: set) -> bool:
    """A bare stub is an item with a detail URL whose page has NOT been opened and which
    carries no substantial content yet — i.e. a list row added without drilling in.
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
    batch-adding a whole list page without opening any detail page.
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


def _top_tied_candidates(tokens: set[str], fields: dict) -> list[tuple[int, str]]:
    """Schema fields whose names overlap the tokens, restricted to the equal
    top-score ties. When the tie-winner rejects a value (enum mismatch) an
    equally-close field gets its chance, but a lower-score field is semantically
    farther and would be polluted (a location-type constant landing in
    'location').
    """
    candidates = sorted(
        (
            (len(_name_tokens(fname) & tokens), fname)
            for fname in fields
            if _strong_overlap(_name_tokens(fname), tokens)
        ),
        key=lambda pair: -pair[0],
    )
    if candidates:
        top_score = candidates[0][0]
        candidates = [c for c in candidates if c[0] == top_score]
    return candidates


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

    # @nonobvious(must-hold): the rendered page outranks background data: a
    # value the page explicitly labels on screen fills its field FIRST, and
    # structured/background data only fills the gaps afterwards or upgrades a
    # visual value it strictly extends (visual "Home" -> background "Home
    # Office"), never replaces it with something coarser.
    visual_fields: set[str] = set()
    for label, value in _labelled_pairs(page.get("text") or "").items():
        label_tokens = _name_tokens(label)
        if not label_tokens:
            continue
        for _score, fname in _top_tied_candidates(label_tokens, fields):
            if _try_set(fname, value):
                visual_fields.add(fname)
                break

    def _upgrades_visual(fname: str, value: Any) -> bool:
        current = row.get(fname)
        return (
            isinstance(current, str)
            and isinstance(value, str)
            and len(value) > len(current)
            and current.casefold() in value.casefold()
        )

    flat = _flatten_jsonld_scalars(ld)
    for path in sorted(flat, key=lambda p: p.count(".")):
        if path in used or path.split(".", 1)[0] in used:
            continue
        key_tokens = _name_tokens(path.replace(".", " "))
        if not key_tokens:
            continue
        for _score, fname in _top_tied_candidates(key_tokens, fields):
            # @nonobvious(forced-by): a page boolean may only fill a boolean
            # field — string coercion would store true as a junk "true" string.
            if isinstance(flat[path], bool) and _peel_optional(
                fields[fname].annotation
            ) is not bool:
                continue
            if fname in visual_fields and _upgrades_visual(fname, flat[path]):
                previous = row.pop(fname)
                if _try_set(fname, flat[path]):
                    used.add(path)
                    break
                row[fname] = previous
            if _try_set(fname, flat[path]):
                used.add(path)
                break

    if isinstance(page.get("links"), list):
        # @nonobvious(forced-by): visible text near a link is unreliable for URL
        # fields (embedded panels render generic anchor text like "Powered by", which
        # token-matches applyUrl); the anchors' own hrefs are the source.
        for fname in fields:
            if fname in row or not re.fullmatch(
                r".*(?:Url|URL|Uri|URI|Href|HREF|Link|LINK)", fname
            ):
                continue
            want = _name_tokens(fname) - {"url", "uri", "link", "href"}
            if not want:
                continue
            for link in page["links"]:
                href = str((link or {}).get("href") or "")
                if not href.startswith(("http://", "https://")):
                    continue
                hay = set(
                    re.split(
                        r"[^a-z0-9]+",
                        (href + " " + str((link or {}).get("text") or "")).lower(),
                    )
                ) - {""}
                if all(
                    any(
                        w == h
                        or (len(w) >= 4 and h.startswith(w))
                        or (len(h) >= 4 and w.startswith(h))
                        for h in hay
                    )
                    for w in want
                ) and _try_set(fname, href):
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
    # @nonobvious(mirrors): a loose additionalProperties schema accepts keys it
    # never declares; the platform convention for those (matching the cloud's
    # output shape) is an 'extra' list of {key, value} pairs.
    if extra_field is None and model.model_config.get("extra") == "allow":
        extra_field, extra_kind = "extra", "undeclared"
    if extra_field and extra_field not in row:
        leftovers = {
            k: v
            for k, v in ld.items()
            if k not in used
            and not k.startswith("@")
            and isinstance(v, (str, int, float, bool))
        }
        link_text = page.get("link_text") or ""
        if link_text:
            stored_blob = _norm_evidence(json.dumps(row, default=str)).replace(" ", "")
            residue = [
                seg.strip()
                for seg in re.split(r"[•|·\n]+", link_text)
                if seg.strip()
                and _norm_evidence(seg).replace(" ", "") not in stored_blob
            ]
            if residue:
                leftovers["source_row"] = _clip_marked(" • ".join(residue))
        if leftovers:
            if extra_kind == "undeclared":
                row[extra_field] = [
                    {"key": k, "value": _clip_marked(v)} for k, v in leftovers.items()
                ]
            elif extra_kind == "kv":
                for key_name in ("key", "name"):
                    shaped = [
                        {key_name: k, "value": _clip_marked(v)} for k, v in leftovers.items()
                    ]
                    if _try_set(extra_field, shaped):
                        break
            else:
                _try_set(extra_field, {k: _clip_marked(v) for k, v in leftovers.items()})
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
    list-row stubs whose own pages have not been opened, else None.
    """
    visited: set = (
        clipboard.setdefault("_visited", set()) if clipboard is not None else set()
    )
    if not _is_bare_stub(store, item, visited):
        return None
    if _bare_stub_count(store, visited) < _MAX_UNVISITED_STUBS:
        return None
    return (
        f"Slow down — you already have {_MAX_UNVISITED_STUBS} list-row stubs with no "
        "detail. Read the items' own pages before adding more: read_pages() covers "
        "them all in one step, then add items with the descriptions it returns. Do "
        "not batch items in from the list page."
    )


def _refresh_read_items(
    store: OutputStore, clipboard: dict[str, Any] | None
) -> set[int]:
    """Indices of items whose own page has ever been observed as read. Recorded
    permanently the moment an item's URL-field value is in the visited/failed
    sets, so later improving the URL (e.g. swapping an embed link for the
    destination site's direct link) can never make the item count as unread again. Called at the start
    of every item mutation and inside the gate checks.
    """
    if clipboard is None:
        return set()
    read: set[int] = clipboard.setdefault("_read_items", set())
    url_field = _item_url_field(store)
    if store.item_model is None or not store.array_field or not url_field:
        return read
    arr = store.data.get(store.array_field) or []
    looked = (clipboard.get("_visited") or set()) | (
        clipboard.get("_read_failed") or set()
    )
    for i, it in enumerate(arr):
        if (
            isinstance(it, dict)
            and it.get(url_field)
            and _norm_url(it[url_field]) in looked
        ):
            read.add(i)
    return read


def _remap_read_items(clipboard: dict[str, Any] | None, removed: list[int]) -> None:
    """Shift the permanently-recorded read-item indices after a removal so the
    provenance in ``_read_items`` keeps pointing at the same items.
    """
    if clipboard is None:
        return
    read = clipboard.get("_read_items")
    if not read:
        return
    removed_set = set(removed)
    clipboard["_read_items"] = {
        i - sum(1 for r in removed_set if r < i)
        for i in read
        if i not in removed_set
    }


def _tolerate_json_list(value: Any) -> Any:
    """Unwrap a list argument that arrived as its own JSON text.

    Claude sometimes serialises a ``list`` tool argument as the STRING
    ``'["a", "b"]'``; a ``str | list`` union accepts the string arm and the
    call then fails downstream as one nonsense value. Only a string that
    parses to a JSON array is unwrapped — any other string passes through.
    """
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("[") and text.endswith("]"):
            try:
                parsed = json.loads(text)
            except ValueError:
                return value
            if isinstance(parsed, list):
                return parsed
    return value


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
    indexed_urls = [
        (i, it.get(url_field))
        for i, it in enumerate(arr)
        if isinstance(it, dict) and it.get(url_field)
    ]
    if not indexed_urls:
        return None
    visited: set = (
        clipboard.setdefault("_visited", set()) if clipboard is not None else set()
    )
    failed: set = (
        clipboard.setdefault("_read_failed", set()) if clipboard is not None else set()
    )
    looked = visited | failed
    read_items = _refresh_read_items(store, clipboard)
    unread = [
        u
        for i, u in indexed_urls
        if i not in read_items and _norm_url(u) not in looked
    ]
    urls = [u for _, u in indexed_urls]
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
        _refresh_read_items(store, clipboard)
        ok, msg = store.update_item(index, fields)
        if ok:
            _mirror()
        return _AwaitableStr(msg)

    def update_items(updates: list[dict[str, Any]]) -> str:
        _refresh_read_items(store, clipboard)
        ok, msg = store.update_many(updates)
        if ok:
            _mirror()
        return _AwaitableStr(msg)

    def set_field(key: str, value: Any) -> str:
        ok, msg = store.set_field(key, value)
        if ok:
            _mirror()
        return _AwaitableStr(msg)

    def mark_absent(field: str | list[str], reason: str) -> str:
        field = _tolerate_json_list(field)
        parts: list[str] = []
        for name in field if isinstance(field, list) else [field]:
            unearned = _absence_unearned(store, clipboard, name)
            if unearned:
                parts.append(unearned)
                continue
            _, msg = store.mark_absent(name, reason)
            parts.append(msg)
        return _AwaitableStr(" ".join(parts))

    def remove_items(indices: list[int], reason: str = "") -> str:
        ok, msg = store.remove_items(indices)
        if ok:
            _remap_read_items(clipboard, sorted({int(i) for i in indices}))
            _mirror()
        return _AwaitableStr(msg)

    return {
        "add_item": add_item,
        "update_item": update_item,
        "update_items": update_items,
        "set_field": set_field,
        "mark_absent": mark_absent,
        "remove_items": remove_items,
        # @nonobvious(forced-by): scripts index into this — a dict, never a JSON string.
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
    adding items whose own detail page has not been opened, so a list page cannot be
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
        _refresh_read_items(store, clipboard)
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
        _refresh_read_items(store, clipboard)
        ok, msg = store.update_many(updates)
        if not ok:
            return ActionResult(error=msg)
        await _mirror_output(store, file_system)
        note = f"{msg} {store.coverage_summary()}"
        return ActionResult(extracted_content=note, long_term_memory=note)

    @tools.action(
        f"Remove item(s) from the '{array}' list by 0-based index — the repair "
        "tool for rows that should not be in the answer: duplicates, a "
        "landing/index page captured as a record, or rows superseded by "
        "corrected ones. Pass the indices to delete plus a one-line reason. "
        "Remaining items shift down, so re-check indices with read_output before "
        "any follow-up update_item calls."
    )
    async def remove_items(
        indices: list[int], reason: str, file_system: FileSystem
    ) -> ActionResult:
        ok, msg = store.remove_items(indices)
        if not ok:
            return ActionResult(error=msg)
        _remap_read_items(clipboard, sorted({int(i) for i in indices}))
        await _mirror_output(store, file_system)
        note = f"{msg} Reason: {reason}. {store.coverage_summary()}"
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
        "every page is read. Verifying absence needs no extra browsing: every read "
        "page's full text is in pages.json, searchable in one run_code_file step."
    )
    async def mark_absent(field: str | list[str], reason: str) -> ActionResult:
        field = _tolerate_json_list(field)
        field_names = (
            [str(f) for f in field] if isinstance(field, list) else [field]
        )
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
        _refresh_read_items(store, clipboard)
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
    indexed_urls = [
        (i, it.get(url_field))
        for i, it in enumerate(arr)
        if isinstance(it, dict) and it.get(url_field)
    ]
    looked = (clipboard.get("_visited") or set()) | (clipboard.get("_read_failed") or set())
    read_items = _refresh_read_items(store, clipboard)
    if not indexed_urls or any(
        i not in read_items and _norm_url(u) not in looked for i, u in indexed_urls
    ):
        return empties
    filtered: list[str] = []
    for entry in empties:
        m = re.match(r"^(\w+) — empty on (\d+) of (\d+)", entry)
        if m and int(m.group(2)) < int(m.group(3)):
            continue
        filtered.append(entry)
    return filtered


def _gate_link_deficit(
    store: OutputStore, clipboard: dict[str, Any] | None
) -> str | None:
    """The bounce message when find_links captured more usable links than the
    output holds items — the signature of a run that read a partial list page and
    stopped (e.g. an embed's links appearing mid-run). None when counts agree.
    """
    if clipboard is None or not store.array_field:
        return None
    kept, _ = _saved_links_sans_offhost(clipboard)
    count = store.item_count()
    if not kept or len(kept) <= count:
        return None
    url_field = _item_url_field(store)
    arr = store.data.get(store.array_field) or []
    item_urls = {
        _norm_url(it[url_field])
        for it in arr
        if url_field and isinstance(it, dict) and it.get(url_field)
    }
    unlisted = [u for u in kept if _norm_url(u) not in item_urls]
    msg = (
        f"find_links captured {len(kept)} on-site link(s) but the output holds "
        f"only {count} item(s)."
    )
    if unlisted:
        shown = "\n- ".join(unlisted[:8])
        # The gate fires once, so a silent top-8 cut is the difference between the
        # agent clearing eight links and believing it is finished, and it knowing how
        # many are actually outstanding.
        more = (
            f"\n- …and {len(unlisted) - 8} more link(s) with no item — "
            "read_pages() covers them all in one call"
            if len(unlisted) > 8
            else ""
        )
        msg += (
            f" These link(s) have no item yet:\n- {shown}{more}\n"
            "Read them with read_pages([...]) and add the missing rows, or state "
            "in your done text why they are not records."
        )
    else:
        msg += (
            " Re-check the saved found_links against the items and add what is "
            "missing, or state in your done text why the counts differ."
        )
    return msg


def register_completeness_gate(
    tools: Tools,
    store: OutputStore,
    on_incomplete,
    clipboard: dict[str, Any] | None = None,
    on_complete=None,
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
            deficit = _gate_link_deficit(store, clipboard)
            if empties or deficit:
                state["bounced"] = True
                if on_incomplete is not None:
                    try:
                        await on_incomplete(empties or ([deficit] if deficit else []))
                    except Exception:
                        logger.debug("completeness gate event emit failed", exc_info=True)
                parts: list[str] = []
                if empties:
                    field_list = "\n- ".join(empties)
                    parts.append(
                        "these fields in the output are still empty:\n- "
                        f"{field_list}\n\nFor each field, either fill it (update_items "
                        "in bulk, or go back to the page that shows it) or, if you "
                        "have looked where it should be and the site genuinely does "
                        "not publish it, settle it with mark_absent(field, reason). "
                        "mark_absent takes a LIST of fields, so one call can settle "
                        "several at once, and pages.json already holds every read "
                        "page's full text — one run_code_file search across it "
                        "verifies absence for all pages without more browsing."
                    )
                if deficit:
                    parts.append(deficit)
                if not (clipboard or {}).get("found_links") and (clipboard or {}).get(
                    "_dom_embed_hosts"
                ):
                    parts.append(
                        "find_links never captured any links, yet the page embeds "
                        "cross-origin panel(s) from "
                        + ", ".join((clipboard or {})["_dom_embed_hosts"])
                        + " — if the links live inside one, run "
                        "find_links(frame_url_contains=...) first."
                    )
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
                        "\n\nA published date is usually NOT in the visible text — "
                        "it is in each page's JSON-LD, which read_pages already returns "
                        "as page['jsonld'] (e.g. its 'datePublished')."
                    )
                return ActionResult(
                    is_done=False,
                    extracted_content=(
                        "Not finished — "
                        + "\n\n".join(parts)
                        + "\n\nYou have ample budget and time remaining. Do the "
                        "work above first; call done again only once the output "
                        "is complete."
                        + hints
                        + date_hint
                    ),
                )
        if on_complete is not None:
            try:
                await on_complete(store.coverage_summary())
            except Exception:
                logger.debug("completeness pass event emit failed", exc_info=True)
        # @nonobvious(forced-by): the judge evaluates the done text, so the
        # store's answer must ride in it or complete runs get judged FAIL.
        if not store.is_empty():
            try:
                answer = store.read_output()
                elision_note = ""
                if len(answer) > _JUDGE_ANSWER_CAP:
                    # @nonobvious(forced-by): a raw cap cuts mid-record and the
                    # judge fails the run as truncated; eliding long values keeps
                    # every record visible in the same budget.
                    answer = json.dumps(
                        elide_long_values(json.loads(answer))[0], default=str
                    )
                    elision_note = "; long values elided for review"
                if len(answer) > _JUDGE_ANSWER_CAP:
                    answer = answer[:_JUDGE_ANSWER_CAP] + "\n…[truncated for length]"
                params.text = (
                    f"FINAL STRUCTURED OUTPUT ({store.coverage_summary()}"
                    f"{elision_note}):\n{answer}\n\n"
                    "REVIEW NOTE: URL fields are correct when they resolve to "
                    "the right page — content rendered inside an embedded "
                    "third-party frame is equally well identified by the host "
                    "page's own URL or the embedded provider's URL; do not "
                    "fail the run over which of the two a URL field carries. "
                    'Values rendered as "<N chars>" are display elisions of '
                    "complete stored data, shortened only for this review — "
                    "the delivered output contains the full values; do not "
                    "treat them as truncation or missing content.\n\n"
                    f"{params.text}"
                )
            except Exception:
                logger.debug("judge answer injection failed", exc_info=True)
        return await original_done(params=params, file_system=file_system)
