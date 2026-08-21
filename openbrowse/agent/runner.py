"""Agent execution engine — wraps browser-use Agent with real-time message streaming."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from openai import APIConnectionError, APIStatusError, RateLimitError

from browser_use import Agent, BrowserSession, ChatAnthropic, ChatOpenAI, Tools
from browser_use.llm import UserMessage
from browser_use.llm.exceptions import (
    ModelOutputTruncatedError,
    ModelProviderError,
    ModelRateLimitError,
)
from browser_use.llm.openai.responses_serializer import ResponsesAPIMessageSerializer
from browser_use.llm.schema import SchemaOptimizer
from browser_use.llm.views import ChatInvokeCompletion, ChatInvokeUsage

from openbrowse.agent import cost, live
from openbrowse import system_metrics
from openbrowse.agent.code_stream import CodeStreamObserver
from openbrowse.agent.activity import (
    clear_activity,
    release_profile,
    session_ended,
    session_started,
    set_activity,
    try_claim_profile,
)
from openbrowse.agent.leak_repair import (
    coerce_action_param_shapes,
    is_missing_action_error,
    mistyped_action_params,
    repair_anthropic_message,
)
from openbrowse.agent.output_store import OutputStore
from openbrowse.agent.schema import json_schema_to_pydantic
from openbrowse.agent.textguard import guard_key
from openbrowse.agent.captcha import install_captcha_bridge, register_captcha_tools
from openbrowse.agent.tools import (
    note_read_action,
    TabManager,
    _eval_js,
    _gate_empty_fields,
    action_param_kinds,
    register_clipboard_tools,
    register_code_tools,
    register_completeness_gate,
    register_fetch_tool,
    register_find_elements_flow,
    register_output_guard_overrides,
    register_output_store_tools,
    register_search_page_flow,
    register_tab_tools,
    register_upload_path_resolution,
)
from openbrowse.browser.factory import display_manager, launch_chrome, stop_chrome
from openbrowse.config import settings
from openbrowse.db import crud

logger = logging.getLogger(__name__)

ONE_M_BETA = "context-1m-2025-08-07"

# @nonobvious(mirrors): re-exported so callers that reach for the running agent
# keep importing it from the runner while the registry itself lives in live.
get_live_agent = live.get_live_agent

_REASONING_PUSH_INTERVAL_S = 0.15
_REASONING_LABEL = "Thinking"
# @nonobvious(mirrors): the live stream and the persisted reasoning row are the
# same text rendered twice, so they clip at the same point — a longer live cap
# would make the card visibly shrink when the settled row takes over.
_REASONING_MAX_CHARS = 6000

_CARDS_EXTENSION = (
    "Every step, before you act, fill three one-sentence fields: what_i_see (what is "
    "actually on the page now), plan_to_goal (how you get from here to the goal), and "
    "next_move (your next single move). Then emit the action."
)

_DRILL_IN_EXTENSION = (
    "Index and results pages are a table of contents, not the content. Never "
    "record an item from its snippet; open its own page and read it first."
)

_TOOLS_EASIEST_EXTENSION = (
    "Browsing here is easiest with your own tools, and extraction has ONE golden "
    "path: (1) find_links(...) collects a page's links with a selector "
    "(href_contains, href_regex, frame_url_contains, container_index, attr) — the "
    "only action that reads links inside an embedded/cross-origin panel; (2) "
    "read_pages() reads every found link in parallel tabs in ONE step, saves "
    "{url, title, text, jsonld, links} per page to pages.json AND prefills "
    "rows_draft.json with one schema row per page; (3) "
    "add_items_from_file('rows_draft.json') loads them all — write NO mapping "
    "script; (4) fix judgement fields in ONE update_items call, deciding from what "
    "you have already read (each page's source-row text is in page['link_text'] "
    "in pages.json) — never write a parser script for prose, and NEVER guess an "
    "enum or default one: a value the page does not state stays null; (5) "
    "mark_absent any field NO page publishes — a field found on some pages with "
    "the rest read is already complete as a partial — then done. A record's real "
    "detail lives only on its own page, never the "
    "list page — add_item refuses more than two undetailed list rows. Use "
    "open_tabs/goto_tab/open_in_new_tab/close_tab only when you must interact with "
    "a page; find_elements and evaluate see only the MAIN page, while a script can "
    "read inside an embed with browser.frame_text(url_part)."
)

def _full_toolbox_extension(tools: Tools) -> str:
    """The definitive action inventory, stated outright. The only other place the
    full toolset appears is the JSON schema at the tail of a very long prompt, and
    a no-reasoning model reads a curated tools section as exhaustive — it will
    refuse a task naming a real action it believes does not exist."""
    names = ", ".join(sorted(tools.registry.registry.actions))
    return (
        "Your complete action list for this session is: "
        f"{names}. Every action on this list exists and works here, whether or "
        "not it is described above. When the task names one of these actions, "
        "call that action; never report a tool as unavailable when it is on "
        "this list."
    )

_CODE_REUSE_EXTENSION = (
    "Any code you write is a reusable script: parameterise it so it works on every "
    "similar/templated page and run it with run_code_file(name, code=…) — it saves "
    "AND runs in one step; re-run with run_code_file(name). Never write one-off "
    "code per page, and never call a site's backend/JSON API from a script — read "
    "the rendered page or its embedded data instead."
)

_OVERLAY_EXTENSION = (
    "A blocking overlay (cookie banner, consent prompt, modal, age gate) is only "
    "dismissed when it STAYS gone: judge success by it not returning after your next "
    "navigation, never by it merely disappearing — overlays can close on a missed "
    "click without recording your choice. If it reappears, your click missed or the "
    "site did not save the decision: find its button on the CURRENT page and click "
    "that, never a remembered element from an earlier step. If it still returns after "
    "two more attempts, stop fighting it and continue the task, ignoring the overlay "
    "unless it actually covers something you must click or read. And never conclude an "
    "on-screen control does not exist because a DOM search cannot find it — some "
    "overlays are invisible to DOM queries yet fully clickable; what the screenshot "
    "shows is real."
)

_CAPTCHA_EXTENSION = (
    "The operator has configured and pays for an authorised CAPTCHA-solving service. "
    "solve_captcha is yours to call: call it immediately when any CAPTCHA, slider, "
    "image grid, icon puzzle, or 'unusual traffic' challenge appears, before clicking "
    "the widget, selecting tiles, or dragging anything. The action handles secondary "
    "interactive puzzles too; wait for its result and never operate those puzzles "
    "manually. Switching sites is a retreat, so reroute only after the solver reports "
    "that the page still refuses. A solution is written into the page and the widget "
    "may still look unsolved, so do not solve twice based on appearance. When the "
    "solver says the solution is placed, submit the form and judge success only by "
    "what the page reports in reply."
)

_CAPTCHA_UNAVAILABLE_EXTENSION = (
    "No CAPTCHA is solved for you here, and none can be solved in this session: "
    "there is no solver configured, so there is no action that will get you past a "
    "challenge. If one blocks the page you need, say so plainly and say what it "
    "blocked. Reaching a different page instead is a partial result, not a success, "
    "so never report the task as done because you found a way around the block."
)

_CLIPBOARD_EXTENSION = (
    "You carry a clipboard: remember(key, value) keeps anything you need to remember "
    "between pages, recall(key) brings it back, and startUrl is already stored there."
)

_OUTPUT_STORE_EXTENSION = (
    "YOUR PURPOSE IS TO DISCOVER, SCRAPE, AND OUTPUT – NOT TO MEMORIZE THINGS. The "
    "user's schema already sits in the output, empty and waiting; the moment you find "
    "something that belongs in it, put it there with add_item or update_item, and let "
    "read_output and search_output do the remembering."
)

_VERIFY_EXTENSION = (
    "The coverage summary printed after every store write IS your verification — "
    "when it shows every field filled, partial-with-all-pages-read, or marked "
    "absent, call done directly; do not re-read the output first. Treat an "
    "empty-on-all field as unfinished work: go back to the page that could fill "
    "it, or — once you have looked and the site genuinely does not publish it — "
    "settle it with mark_absent(field, reason)."
)

_ACTION_CONTRACT_EXTENSION = (
    "Every reply must include the 'action' field — prose fields like plan_update "
    "and next_goal describe intent but execute nothing."
)

_BEGIN_EXTENSION = (
    "Begin your browsing by recalling startUrl and opening that page in a new tab."
)

_GOAL_PROMPT = (
    "Reply with one sentence stating this task's goal: what a complete and "
    "correct result looks like, in the task's own words. Start directly with "
    "the substance (never with a phrase like 'the goal is'); name the purpose, "
    "not the output's shape; do not list fields or restate the schema."
)


_STALE_CAPTCHA_CLAIMS: tuple[str, ...] = (
    "5. **Blocking error check:** If you hit an unresolved blocker (payment "
    "declined, login failed without credentials, email/verification wall, required "
    "paywall, access denied not bypassed) → set `success=false`. Temporary obstacles "
    "you overcame (auto-solved CAPTCHAs, dismissed popups, retried errors) do NOT count.",
    "CAPTCHAs are automatically solved by the browser. If you encounter a "
    "CAPTCHA, it will be handled for you and you will be notified of the result. "
    "Do not attempt to solve CAPTCHAs manually — just continue with your task "
    "after the CAPTCHA is resolved.",
    "CAPTCHAs are handled automatically.",
    "CAPTCHAs are solved automatically.",
    "auto-solved CAPTCHAs",
    "Captcha appeared twice on this site. Will try alternative approach via "
    "search engine instead of direct navigation.",
)

_SOLVING_REPLACEMENTS: tuple[str, ...] = (
    "5. **Blocking error check:** If you hit an unresolved blocker (payment "
    "declined, login failed without credentials, email or non-CAPTCHA verification "
    "wall, required paywall, access denied not bypassed) → set `success=false`. "
    "Temporary obstacles you overcame (CAPTCHAs solved with solve_captcha, dismissed "
    "popups, retried errors) do NOT count.",
    "No CAPTCHA is solved on your behalf here. When you hit one, solve it "
    "yourself with the solve_captcha action and then carry on with your task.",
    "You must solve CAPTCHAs yourself with the solve_captcha action.",
    "You must solve CAPTCHAs yourself with the solve_captcha action.",
    "CAPTCHAs you solved",
    "Captcha appeared twice on this site. Solved it with solve_captcha both "
    "times and carried on.",
)

_UNSOLVABLE_REPLACEMENTS: tuple[str, ...] = (
    "5. **Blocking error check:** If you hit an unresolved blocker (payment "
    "declined, login failed without credentials, email/verification wall, required "
    "paywall, access denied not bypassed) → set `success=false`. Temporary obstacles "
    "you overcame (dismissed popups or retried errors) do NOT count; an unsolved "
    "CAPTCHA remains a blocker.",
    "No CAPTCHA is solved on your behalf here, and none can be solved in this "
    "session. When you hit one, report plainly what it blocked rather than "
    "treating a way around it as success.",
    "CAPTCHAs cannot be solved in this session.",
    "CAPTCHAs cannot be solved in this session.",
    "CAPTCHAs that blocked you",
    "Captcha appeared twice on this site. Reported it as a block rather than "
    "calling a way around it a success.",
)


def _captcha_claim_fixes(solving_available: bool) -> tuple[tuple[str, str], ...]:
    """Upstream's captcha claims paired with what is actually true of this session."""
    replacements = (
        _SOLVING_REPLACEMENTS if solving_available else _UNSOLVABLE_REPLACEMENTS
    )
    return tuple(zip(_STALE_CAPTCHA_CLAIMS, replacements))


def _captcha_corrected_system_prompt(
    llm: Any,
    max_actions: int,
    *,
    solving_available: bool,
    use_thinking: bool = True,
    flash_mode: bool = False,
) -> tuple[str | None, int]:
    """Upstream's own system prompt with its "CAPTCHAs are solved for you" claims put
    right, and how many of them were found.

    @nonobvious(forced-by): those claims hold only on Browser-Use's cloud browsers,
    whose proxy emits the CDP events the captcha watchdog waits for. Nothing emits
    them here, so the stock prompt forbids the one action that gets past a challenge.
    Rebuilding through upstream's SystemPrompt keeps whichever template it would have
    picked, and keeps inheriting its later edits.
    """
    try:
        from browser_use.agent.prompts import SystemPrompt

        model_name = str(getattr(llm, "model", "") or "")
        message = SystemPrompt(
            max_actions_per_step=max_actions,
            # @nonobvious(must-hold): these two pick the template, and the Agent parses
            # the model's replies against the schema its own template describes, so a
            # rebuild that guesses them can install a prompt for the wrong schema.
            use_thinking=use_thinking,
            flash_mode=flash_mode,
            is_anthropic=isinstance(llm, ChatAnthropic),
            is_browser_use_model="browser-use/" in model_name.lower(),
            model_name=model_name,
        ).get_system_message()
        base = message.content
    except Exception:
        logger.warning("could not rebuild the system prompt", exc_info=True)
        return None, 0
    if not isinstance(base, str):
        return None, 0
    hits = 0
    for stale, corrected in _captcha_claim_fixes(solving_available):
        found = base.count(stale)
        if found:
            hits += found
            base = base.replace(stale, corrected)
    return base, hits


_STALE_CAPTCHA_CLAIM_RE = re.compile(
    r"CAPTCHAs?[^.\n]{0,90}?(automatically|handled for you|solved for you)", re.I
)


class BudgetExceededError(Exception):
    """Raised when a session exceeds its max_cost_usd budget."""


_IMAGE_TOKENS_ESTIMATE = 2000


def _estimate_request_tokens(payload: Any) -> int:
    """Conservative token estimate for an outgoing request: text at ~3 chars
    per token (English runs nearer 4, so this overshoots) plus a flat
    allowance per embedded image, recognised as a long unbroken base64 run."""
    chars = 0
    images = 0
    stack: list[Any] = [payload]
    while stack:
        cur = stack.pop()
        if isinstance(cur, str):
            if len(cur) > 5000 and " " not in cur[:200]:
                images += 1
            else:
                chars += len(cur)
        elif isinstance(cur, dict):
            stack.extend(cur.values())
        elif isinstance(cur, (list, tuple)):
            stack.extend(cur)
        elif hasattr(cur, "model_dump"):
            try:
                stack.append(cur.model_dump())
            except Exception:
                stack.append(str(cur))
        elif cur is not None and not isinstance(cur, (int, float, bool)):
            stack.append(str(cur))
    return int(chars / 3) + images * _IMAGE_TOKENS_ESTIMATE


class _BudgetGuard:
    """Hard budget enforcement at the choke point every model call passes
    through — agent steps, judge reviews and repair retries alike. After each
    call the cumulative spend is rechecked immediately, bounding overshoot to
    a single call even when the next step-boundary check is far away; before
    each call the call's worst case (estimated input plus the client's full
    output allowance, at the model's dearest rates) is reserved against the
    cap, so a call that could cross it is never dispatched and the recorded
    total cannot exceed the budget. The step-end row check remains as the
    backstop that also refreshes the cap, which a keep-alive follow-up may
    raise mid-run."""

    def __init__(
        self,
        model_id: str,
        carried: dict[str, float],
        capsolver_costs: list[float],
        budget: Any,
    ) -> None:
        self.model_id = model_id
        self.carried = carried
        self.capsolver_costs = capsolver_costs
        self.budget = float(budget) if budget else None
        self._token_cost: Any = None

    def bind(self, token_cost_service: Any) -> None:
        self._token_cost = token_cost_service

    def spent(self) -> float:
        history = getattr(self._token_cost, "usage_history", None)
        return (
            self.carried["llm"]
            + self.carried["capsolver"]
            + cost.history_cost(history)
            + sum(self.capsolver_costs)
        )

    def precheck(self, payload: Any, max_output_tokens: Any) -> None:
        if not self.budget or self._token_cost is None:
            return
        worst = cost.worst_case_call_cost(
            self.model_id,
            _estimate_request_tokens(payload),
            int(max_output_tokens or 0) or 16384,
        )
        if worst is None:
            return
        spent = self.spent()
        if spent + worst > self.budget:
            raise BudgetExceededError(
                f"Budget ${self.budget:.2f} cannot cover the next model call "
                f"(spent ${spent:.4f}, worst case +${worst:.4f}) — stopping "
                "before it is made"
            )

    def postcheck(self, usage: Any) -> None:
        if not self.budget:
            return
        # The call that just returned is not yet in usage_history — the
        # token-cost wrapper sits outside this one — so count it directly.
        total = self.spent()
        if usage is not None:
            total += cost.usage_cost(self.model_id, usage)
        if total >= self.budget:
            raise BudgetExceededError(
                f"Cost ${total:.4f} reached budget ${self.budget:.2f}"
            )



def _timeout_text(error: BaseException) -> bool:
    message = str(error).lower()
    return (
        "timeout" in message
        or "timed out" in message
        or "headers timeout" in message
        or "body timeout" in message
    )


def _provider_failure_info(error: BaseException) -> tuple[str, int | None, str] | None:
    if isinstance(error, RateLimitError):
        status_code = getattr(error, "status_code", None) or 429
        return "provider_rate_limit", status_code, "error"
    if isinstance(error, APIConnectionError):
        return "provider_connection_error", None, "error"
    if isinstance(error, APIStatusError):
        status_code = getattr(error, "status_code", None)
        if _timeout_text(error):
            return "provider_timeout", status_code, "error"
        if status_code == 429:
            return "provider_rate_limit", 429, "error"
        if isinstance(status_code, int) and 500 <= status_code <= 599:
            return "provider_server_error", status_code, "error"
        if isinstance(status_code, int):
            return "provider_error", status_code, "error"
    return None


def _failure_info(error: BaseException) -> tuple[str, int | None, str]:
    if isinstance(error, BudgetExceededError):
        return "budget_exceeded", None, "stopped"
    if isinstance(error, asyncio.TimeoutError):
        return "session_timeout", None, "timed_out"
    if isinstance(error, ModelOutputTruncatedError):
        return "invalid_output", None, "error"
    if isinstance(error, ModelRateLimitError):
        return "provider_rate_limit", 429, "error"
    provider_failure = _provider_failure_info(error)
    if provider_failure is not None:
        return provider_failure
    if isinstance(error, ModelProviderError):
        provider_failure = (
            _provider_failure_info(getattr(error, "__cause__", None))
            or _provider_failure_info(getattr(error, "__context__", None))
        )
        if provider_failure is not None:
            return provider_failure
        status_code = getattr(error, "status_code", None)
        if _timeout_text(error):
            return "provider_timeout", status_code, "error"
        if status_code == 429:
            return "provider_rate_limit", 429, "error"
        if isinstance(status_code, int) and 500 <= status_code <= 599:
            return "provider_server_error", status_code, "error"
        if isinstance(status_code, int):
            return "provider_error", status_code, "error"
        return "agent_failure", None, "error"
    if _timeout_text(error):
        return "session_timeout", None, "timed_out"
    return "agent_failure", None, "error"


_MAX_REVIEW_ROUNDS = 3
_MAX_REVIEW_JUSTIFICATIONS = 2
_REVIEW_REASON_CAP = 4000


def _inject_followup_task(agent: Any, message: str) -> None:
    """Queue a follow-up task on an agent between runs, preferring the public
    add_new_task (which also resets follow-up/stopped/paused state and the event
    bus) with the private message-manager as the older-version fallback. Not for
    mid-run injection: a live run must keep its control-flow state, so mid-run
    nudges call the message manager directly instead.
    """
    try:
        agent.add_new_task(message)
    except AttributeError:
        agent._message_manager.add_new_task(message)


def _last_judgement(history: Any) -> Any:
    try:
        return getattr(history.history[-1].result[-1], "judgement", None)
    except (IndexError, AttributeError):
        return None


def _review_message(reason: str, replies_left: int) -> str:
    if replies_left > 0:
        plural = "replies" if replies_left != 1 else "reply"
        return (
            "A reviewer has assessed your submitted result and requests "
            f"changes:\n\n{reason}\n\nIf the review is right, make those "
            "changes now and submit with done again. If you are confident the "
            "review is wrong, you may reply via done explaining why "
            f"({replies_left} {plural} remaining) — do not just restate what "
            "you already said."
        )
    return (
        "The reviewer still requests changes and your replies are used "
        f"up:\n\n{reason}\n\nApply the requested changes now as far as the "
        "site's content allows, then submit with done again. Where the review "
        "asks for something the source genuinely does not publish, state that "
        "explicitly in your done text."
    )


async def _run_with_review(
    agent: Any,
    store: "OutputStore | None",
    session_id: str,
    run_agent,
    review_state: dict[str, Any] | None = None,
) -> Any:
    """Run the agent, then loop while the reviewer requests changes on a run
    that would otherwise complete as a success: the review re-enters the agent
    as a continuation turn. The model may talk back — a continuation that
    leaves the output untouched counts as a justification, and after
    ``_MAX_REVIEW_JUSTIFICATIONS`` of those the message demands the changes.
    ``_MAX_REVIEW_ROUNDS`` bounds the whole conversation; the final verdict is
    recorded either way.
    """
    if review_state is None:
        review_state = {"round": 0, "snapshot": None}
    history = await run_agent()
    justifications = 0
    for _ in range(_MAX_REVIEW_ROUNDS):
        judgement = _last_judgement(history)
        provisional_ok = history.is_done() and (history.is_successful() is not False)
        if judgement is None or bool(judgement.verdict) or not provisional_ok:
            break
        reason = " ".join(
            (judgement.failure_reason or judgement.reasoning or "").split()
        )[:_REVIEW_REASON_CAP]
        if not reason:
            break
        await crud.create_message(
            session_id=session_id,
            role="ai",
            msg_type="event",
            summary=reason,
            data=json.dumps(
                {"category": "judge", "action": "review", "verdict": "changes"}
            ),
            count_step=False,
        )
        snapshot = store.read_output() if store is not None else None
        review_state["round"] += 1
        review_state["snapshot"] = snapshot
        steps_before = len(getattr(history, "history", []) or [])
        replies_left = _MAX_REVIEW_JUSTIFICATIONS - justifications
        message = _review_message(reason, replies_left)
        _inject_followup_task(agent, message)
        history = await run_agent()
        # @nonobvious(must-hold): a round that added no steps means the agent
        # could not act at all (dead browser, wedged loop); re-judging the
        # unchanged trajectory can only repeat the same verdict, so burning
        # the remaining rounds on it is pure cost.
        if len(getattr(history, "history", []) or []) <= steps_before:
            await crud.create_message(
                session_id=session_id,
                role="ai",
                msg_type="event",
                summary=(
                    "Review round produced no agent activity — stopping the "
                    "review conversation and recording the standing verdict"
                ),
                data=json.dumps({"category": "judge", "action": "review"}),
                count_step=False,
            )
            break
        if store is not None and store.read_output() == snapshot:
            justifications += 1
    return history


_STORE_ONLY_ACTIONS = {
    "add_item",
    "update_item",
    "update_items",
    "set_field",
    "mark_absent",
    "remove_items",
    "read_output",
    "search_output",
    "add_items_from_file",
    "update_items_from_file",
    "remember",
    "recall",
    "read_file",
    "write_file",
    "replace_file",
    "run_code_file",
    "read_pages",
    "http_fetch",
}


def _install_lean_state(browser_session: BrowserSession, flag: dict[str, bool]) -> None:
    """Wrap the session's state fetch so that, when the previous step only did
    store/file/sandbox work (``flag['eligible']``) and the page URL is unchanged,
    the next step gets a stub state — URL, title and tabs, but no DOM element list and no
    screenshot — instead of re-serialising the same page. The cached selector map is
    kept, so element indices from the last full state still resolve for clicks.
    One-shot per arming: only the agent's own per-step fetch sees the stub, never a
    call made mid-action.
    """
    from browser_use.browser.views import BrowserStateSummary
    from browser_use.dom.views import SerializedDOMState

    original = browser_session.get_browser_state_summary

    async def lean_get(
        include_screenshot: bool = True,
        cached: bool = False,
        include_recent_events: bool = False,
    ) -> Any:
        if flag.get("eligible"):
            flag["eligible"] = False
            cached_state = getattr(browser_session, "_cached_browser_state_summary", None)
            current_url = None
            try:
                current_url = await _eval_js(browser_session, "window.location.href")
            except Exception:
                logger.debug("lean state: url check failed", exc_info=True)
            if (
                cached_state is not None
                and cached_state.dom_state is not None
                and current_url
                and current_url == cached_state.url
            ):
                return BrowserStateSummary(
                    dom_state=SerializedDOMState(
                        _root=None, selector_map=cached_state.dom_state.selector_map
                    ),
                    url=cached_state.url,
                    title=(
                        f"{cached_state.title} [CODE MODE — browser parked & healthy; "
                        "DOM and screenshot deliberately omitted while you do "
                        "file/store work]"
                    ),
                    tabs=cached_state.tabs,
                    screenshot=None,
                    state_error=(
                        "CODE MODE: your last step was file/store/code work, so no "
                        "screenshot or DOM was captured — the page is parked, healthy "
                        "and unchanged. Skip the visual check: describe your "
                        "file/store progress instead of the page, and never call the "
                        "page empty, stale or stuck. Element indices from the earlier "
                        "state remain valid; any browser action returns the full view."
                    ),
                )
        return await original(
            include_screenshot=include_screenshot,
            cached=cached,
            include_recent_events=include_recent_events,
        )

    # @nonobvious(forced-by): BrowserSession is pydantic extra='forbid'; only
    # object.__setattr__ can shadow the method on the instance.
    object.__setattr__(browser_session, "get_browser_state_summary", lean_get)


async def _settle_code_stream(llm: Any, result: Any, output_format: Any) -> None:
    observer = getattr(llm, "stream_observer", None)
    if observer is None or output_format is None:
        return
    from openbrowse.agent.code_stream import completion_has_run_code_file

    try:
        await observer.settle(
            completion_has_run_code_file(getattr(result, "completion", None))
        )
    except Exception:
        logger.debug("code stream settle failed", exc_info=True)


class _ResponsesChatOpenAI(ChatOpenAI):
    """ChatOpenAI pointed at OpenAI's Responses API instead of chat.completions:
    the Responses endpoint accepts the full reasoning ladder (chat.completions
    rejects "max"). ``reasoning_effort`` here is the Responses effort string, or
    None to omit the parameter so the provider's own default applies.
    ``max_completion_tokens`` maps to ``max_output_tokens``.
    """

    def _build_request(self, messages: Any, output_format: Any) -> dict[str, Any]:
        input_messages = ResponsesAPIMessageSerializer.serialize_messages(messages)
        # @nonobvious(deliberately-missing): no `tools`, ever — Responses
        # built-ins (web_search etc.) must not act outside the browser sandbox.
        params: dict[str, Any] = {
            "model": self.model,
            "input": input_messages,
            "store": False,
        }
        if self.max_completion_tokens is not None:
            params["max_output_tokens"] = self.max_completion_tokens
        if self.reasoning_effort is not None:
            reasoning: dict[str, Any] = {"effort": self.reasoning_effort}
            if self.reasoning_effort != "none" and not getattr(
                self, "_summary_unsupported", False
            ):
                reasoning["summary"] = "auto"
            params["reasoning"] = reasoning
        if (
            output_format is not None
            and self.add_schema_to_system_prompt
            and input_messages
            and input_messages[0].get("role") == "system"
        ):
            json_schema = SchemaOptimizer.create_optimized_json_schema(
                output_format,
                remove_min_items=self.remove_min_items_from_schema,
                remove_defaults=self.remove_defaults_from_schema,
            )
            schema_text = f"\n<json_schema>\n{json_schema}\n</json_schema>"
            content = input_messages[0].get("content", "")
            if isinstance(content, str):
                input_messages[0]["content"] = content + schema_text
            elif isinstance(content, list):
                input_messages[0]["content"] = list(content) + [
                    {"type": "input_text", "text": schema_text}
                ]
        return params

    @staticmethod
    def _output_text(response: Any) -> str:
        text = getattr(response, "output_text", None)
        if text:
            return text
        parts: list[str] = []
        for item in getattr(response, "output", None) or []:
            for part in getattr(item, "content", None) or []:
                part_text = getattr(part, "text", None)
                if part_text:
                    parts.append(part_text)
        return "".join(parts)

    def _raise_if_truncated(self, response: Any) -> None:
        details = getattr(response, "incomplete_details", None)
        if getattr(response, "status", None) == "incomplete" and (
            getattr(details, "reason", None) == "max_output_tokens"
        ):
            raise ModelOutputTruncatedError(
                message=(
                    f"Model output was truncated at max_output_tokens="
                    f"{self.max_completion_tokens}; the structured output is "
                    "incomplete. Increase max_output_tokens or request shorter output."
                ),
                model=self.name,
            )

    @staticmethod
    def _reasoning_summary(response: Any) -> str:
        parts: list[str] = []
        for item in getattr(response, "output", None) or []:
            if getattr(item, "type", None) != "reasoning":
                continue
            for s in getattr(item, "summary", None) or []:
                text = getattr(s, "text", None)
                if text:
                    parts.append(text)
        return " ".join(" ".join(parts).split())

    async def _stream_reasoning_create(self, params: dict[str, Any]) -> Any:
        sid = getattr(self, "_activity_session", None)
        loop = asyncio.get_running_loop()
        parts: list[str] = []
        last_push = 0.0
        began_at: float | None = None
        async with self.get_client().responses.stream(**params) as stream:
            async for event in stream:
                if not sid:
                    continue
                if str(getattr(event, "type", "")).endswith(
                    "reasoning_summary_text.delta"
                ):
                    parts.append(getattr(event, "delta", "") or "")
                    now = loop.time()
                    if began_at is None:
                        began_at = now
                    if now - last_push > _REASONING_PUSH_INTERVAL_S:
                        last_push = now
                        text = " ".join("".join(parts).split())
                        set_activity(
                            sid,
                            _REASONING_LABEL,
                            spin=True,
                            stream=text[:_REASONING_MAX_CHARS],
                            kind="reasoning",
                        )
            self._reasoning_seconds = (
                round(loop.time() - began_at, 1) if began_at is not None else None
            )
            return await stream.get_final_response()

    async def _create(self, params: dict[str, Any]) -> Any:
        if "summary" not in (params.get("reasoning") or {}):
            return await self.get_client().responses.create(**params)
        try:
            return await self._stream_reasoning_create(params)
        except (APIStatusError, APIConnectionError, RateLimitError):
            raise
        except Exception:
            # @nonobvious(forced-by): the SDK's responses.stream surface varies
            # by version; a non-API failure falls back to the plain call.
            logger.debug("responses streaming failed; falling back", exc_info=True)
            return await self.get_client().responses.create(**params)

    async def _create_with_summary_fallback(self, params: dict[str, Any]) -> Any:
        try:
            return await self._create(params)
        except APIStatusError as e:
            # @nonobvious(forced-by): unverified OpenAI orgs 400 on the summary
            # parameter — drop it once and remember, not fail every step.
            if (
                e.status_code == 400
                and "summary" in str(e).lower()
                and "summary" in (params.get("reasoning") or {})
            ):
                self._summary_unsupported = True
                retry_params = dict(params)
                retry_params["reasoning"] = {
                    k: v for k, v in params["reasoning"].items() if k != "summary"
                }
                return await self.get_client().responses.create(**retry_params)
            raise

    def _parse_structured(self, text: str, output_format: Any) -> Any:
        try:
            return output_format.model_validate_json(text)
        except Exception:
            # @nonobvious(forced-by): models append prose after the JSON and
            # output_text concatenates items; the first balanced object wins.
            cleaned = _strip_json_fence(text)
            start = cleaned.find("{")
            if start < 0:
                raise
            obj, _ = json.JSONDecoder().raw_decode(cleaned[start:])
            # @nonobvious(mirrors): the Anthropic client repairs argument shapes
            # before validation; without the same pass here, one slightly
            # mis-shaped argument hard-fails the reply, the correction reads as
            # a rejection of the action itself, and the model durably concludes
            # the tool does not exist.
            param_kinds = getattr(self, "_action_param_kinds", None)
            if isinstance(obj, dict) and param_kinds:
                try:
                    coerce_action_param_shapes(obj, param_kinds)
                except Exception:
                    logger.debug("action param coercion failed", exc_info=True)
            return output_format.model_validate(obj)

    @staticmethod
    def _usage_from_responses(response: Any) -> ChatInvokeUsage | None:
        usage = getattr(response, "usage", None)
        if usage is None:
            return None
        details = getattr(usage, "input_tokens_details", None)
        return ChatInvokeUsage(
            prompt_tokens=usage.input_tokens,
            prompt_cached_tokens=getattr(details, "cached_tokens", None),
            # @nonobvious(forced-by): Responses usage exposes no cache writes.
            prompt_cache_creation_tokens=None,
            prompt_image_tokens=None,
            completion_tokens=usage.output_tokens,
            total_tokens=usage.total_tokens,
        )

    async def ainvoke(self, messages: Any, output_format: Any = None, **kwargs: Any) -> Any:
        sid = getattr(self, "_activity_session", None)
        if sid:
            last = getattr(self, "_last_action", None)
            label = "Model reasoning" + (f" · next step after {last}" if last else "")
            set_activity(sid, label, spin=True, kind="reasoning")
        summary_box = {"text": ""}

        async def _once(msgs: Any) -> Any:
            guard = getattr(self, "_budget_guard", None)
            if guard is not None:
                guard.precheck(msgs, getattr(self, "max_completion_tokens", None))
            params = self._build_request(msgs, output_format)
            response = await self._create_with_summary_fallback(params)
            self._raise_if_truncated(response)
            text = self._output_text(response)
            usage = self._usage_from_responses(response)
            if guard is not None:
                guard.postcheck(usage)
            summary_box["text"] = self._reasoning_summary(response)
            stop_reason = getattr(response, "status", None)
            if output_format is None:
                return ChatInvokeCompletion(
                    completion=text or "", usage=usage, stop_reason=stop_reason
                )
            if not text:
                raise ModelProviderError(
                    message="Failed to parse structured output from model response",
                    status_code=500,
                    model=self.name,
                )
            return ChatInvokeCompletion(
                completion=self._parse_structured(text, output_format),
                usage=usage,
                stop_reason=stop_reason,
            )

        try:
            try:
                result = await _invoke_with_action_repair(_once, messages, output_format)
            except (ModelProviderError, ModelOutputTruncatedError):
                raise
            except RateLimitError as e:
                raise ModelRateLimitError(message=e.message, model=self.name) from e
            except APIConnectionError as e:
                raise ModelProviderError(message=str(e), model=self.name) from e
            except APIStatusError as e:
                raise ModelProviderError(
                    message=e.message, status_code=e.status_code, model=self.name
                ) from e
        finally:
            if sid:
                set_activity(sid, "Running actions", spin=True)
        summary = summary_box["text"]
        if summary:
            self._last_model_reasoning = summary
            self._last_reasoning_seconds = getattr(self, "_reasoning_seconds", None)
            if sid:
                set_activity(
                    sid,
                    _REASONING_LABEL,
                    stream=summary[:_REASONING_MAX_CHARS],
                    seconds=self._last_reasoning_seconds,
                    kind="reasoning",
                )
        await _settle_code_stream(self, result, output_format)
        return result


_MISSING_ACTION_CORRECTION = (
    'Your reply was rejected: it contained no executable "action" field. The prose '
    "fields (thinking, plan_update, next_goal and so on) describe intent but execute "
    'NOTHING — only the "action" list runs. Respond again with the same content plus '
    '"action": [{"<action_name>": {<parameters>}}]. Do not put action parameters at '
    'the top level: to run code, that is '
    '"action": [{"run_code_file": {"name": "script.py", "code": "..."}}]. If you '
    "sent several separate tool calls, that is the error: send exactly ONE tool "
    'call whose "action" array lists every action in order.'
)
_MISSING_ACTION_FINAL = (
    'Rejected again: still no valid "action" field. Reply now with minimal prose and '
    'the "action" list — e.g. {"thinking": "...", "action": [{"<action_name>": '
    "{<parameters>}}]}. Nothing you write executes without it."
)


def _mistyped_correction(detail: str) -> str:
    return (
        "Your reply was rejected because these action ARGUMENTS had the wrong "
        f"type: {detail}. The action itself exists and is valid — never conclude "
        "from this message that a tool is unavailable, and do not drop the "
        "action. Resend the same reply with each argument as its real JSON "
        "type: a list must be a JSON array (not that array quoted as a "
        "string), an object a JSON object, a number a bare number."
    )


_EXTRA_INPUT_RE = re.compile(r"\.(\w+): Extra inputs are not permitted")


def _actions_absent_from_schema(detail: str) -> list[str]:
    """Action names the reply used that no schema variant accepts. When an
    action exists but its arguments are mis-shaped, its own variant reports a
    field-level error (a deeper ``.name.`` path) alongside the other variants'
    "extra inputs" complaints; when the name is absent from the schema, every
    variant flags it as extra and none goes deeper — telling the model to fix
    its argument types would then be false and teaches it nothing."""
    absent = []
    for name in sorted(set(_EXTRA_INPUT_RE.findall(detail))):
        if f".{name}." in detail:
            continue
        if f"{name.title().replace('_', '')}ActionModel" in detail:
            continue
        absent.append(name)
    return absent


def _unknown_action_correction(names: list[str]) -> str:
    listed = ", ".join(repr(n) for n in names)
    return (
        f"Your reply was rejected because it used an action that is not in this "
        f"session's action schema: {listed}. That action cannot run here no "
        "matter how its arguments are shaped — do not retry it and do not "
        "invent a replacement name. Your system prompt lists every available "
        "action; resend your reply using the closest action from that list."
    )


def _restore_screenshot_action(tools: Any, action_entry: Any, agent: Any) -> None:
    """browser-use's Agent constructor strips the ``screenshot`` action from
    the registry whenever ``use_vision != 'auto'``, treating it as a redundant
    observation aid — but the action is also the only way to SAVE a screenshot
    to a file, and the system prompt's action inventory was generated before
    the strip, so the model is promised a tool the schema then lacks. Put the
    entry back and rebuild the action models the constructor derived from the
    stripped registry; later per-page rebuilds inherit the restored entry."""
    if action_entry is None:
        return
    actions = tools.registry.registry.actions
    if "screenshot" in actions:
        return
    actions["screenshot"] = action_entry
    try:
        agent._setup_action_models()
        # The constructor parsed initial_actions with the classes the rebuild
        # just replaced; validating stale instances against the new AgentOutput
        # fails every session at startup. Re-convert them the way upstream's own
        # skill registration does after the same rebuild.
        if getattr(agent, "initial_actions", None):
            agent.initial_actions = agent._convert_initial_actions(
                [a.model_dump(exclude_unset=True) for a in agent.initial_actions]
            )
    except Exception:
        logger.warning(
            "action model rebuild after screenshot restore failed", exc_info=True
        )


_NARRATIVE_FIELDS = (
    "evaluation_previous_goal",
    "memory",
    "next_goal",
    "what_i_see",
    "plan_to_goal",
    "next_move",
)

_MAX_NARRATIVE_RETRIES = 2

_BLANK_NARRATIVE_CORRECTION = (
    "Your reply left every narrative field empty. Those fields are how you tell your "
    "next step what you just did and why — a step that fills none of them arrives in "
    "your history as a bare tool result with no record of your intent, and you will "
    "repeat yourself. Resend the same action, and fill what_i_see, plan_to_goal and "
    "next_move with one real sentence each. Not an empty string, not a placeholder."
)


def _has_no_narrative(completion: Any) -> bool:
    """True when a reply left every narrative field blank.

    The schema marks these required, but 'required' means the key is present, not that
    the value says anything, so a model can satisfy it with empty strings.
    """
    if completion is None:
        return False
    if not any(hasattr(completion, f) for f in _NARRATIVE_FIELDS):
        return False
    return not any(str(getattr(completion, f, "") or "").strip() for f in _NARRATIVE_FIELDS)


async def _invoke_with_action_repair(
    invoke: Any, messages: Any, output_format: Any
) -> Any:
    """Run ``invoke(messages)`` and, when the reply omits the executable action
    field or types an action argument wrongly, retry up to twice with a
    corrective user message naming the actual defect. A third failure raises a
    short instructive error instead of the raw pydantic dump. Shared by the
    Anthropic and OpenAI clients — the failure modes are identical.
    """
    extra: list[Any] = []
    last_detail = ""
    attempt = -1
    narrative_retries = 0
    # @nonobvious(must-hold): the narrative retry has its own budget. Sharing the
    # action-repair attempts would mean one prose-less reply costs a later mis-typed
    # action its last chance, and that path abandons the step outright.
    while attempt < 2:
        attempt += 1
        try:
            result = await invoke(list(messages) + extra)
            if narrative_retries >= _MAX_NARRATIVE_RETRIES or not _has_no_narrative(
                getattr(result, "completion", None)
            ):
                return result
            # The schema requires these fields but is satisfied by empty strings, and a
            # step that says nothing lands in history as its action's result alone. Ask
            # again rather than accept it; accept once the budget is spent, because a
            # model that will not write prose should not kill an otherwise working run.
            narrative_retries += 1
            attempt -= 1
            logger.info(
                "Retrying LLM call after a reply with no narrative (%d/%d)",
                narrative_retries,
                _MAX_NARRATIVE_RETRIES,
            )
            extra.append(UserMessage(content=_BLANK_NARRATIVE_CORRECTION))
            continue
        except Exception as e:
            if output_format is None:
                raise
            detail = mistyped_action_params(e)
            if detail:
                last_detail = detail
                absent = _actions_absent_from_schema(detail)
                correction = (
                    _unknown_action_correction(absent)
                    if absent
                    else _mistyped_correction(detail)
                )
            elif is_missing_action_error(e):
                correction = (
                    _MISSING_ACTION_CORRECTION if attempt == 0 else _MISSING_ACTION_FINAL
                )
            else:
                raise
            if attempt == 2:
                # @nonobvious(forced-by): the raw pydantic dump would replay
                # into every later step's context; keep the failure cheap.
                if last_detail:
                    raise ValueError(
                        "This step was abandoned after three replies whose "
                        f"action arguments were mis-typed: {last_detail}. Send "
                        "each argument as its real JSON type."
                    ) from e
                raise ValueError(
                    "Your reply omitted the executable 'action' field "
                    "three times, so this step was abandoned. Nothing "
                    "runs without \"action\": [{\"<action_name>\": "
                    "{...params}}] — include it in your next reply."
                ) from e
            logger.info(
                "Retrying LLM call after %s (attempt %d)%s",
                "mis-typed action arguments" if detail else "missing/malformed action",
                attempt + 1,
                f": {detail}" if detail else "",
            )
            extra.append(UserMessage(content=correction))


class _RepairingChatAnthropic(ChatAnthropic):
    """ChatAnthropic hardened four ways: (1) recover the action list Claude
    sometimes serialises into the AgentOutput ``thinking`` field so the forced
    tool call validates without dropping ``thinking``; (2) retry once with a
    correction if a leak can't be salvaged; (3) recover from output truncation by
    retrying once with streaming + a higher ``max_tokens`` (the non-streaming API
    refuses >~16k, and browser-use's own retry re-runs at the same cap forever);
    (4) forbid parallel tool calls under auto tool choice, and merge them back
    into one structured call if the model still splits.
    """

    async def _create_message(self, **params: Any) -> Any:
        tool_choice = params.get("tool_choice")
        if isinstance(tool_choice, dict) and tool_choice.get("type") == "auto":
            # @nonobvious(forced-by): extended thinking forces auto tool choice,
            # under which Claude may split its reply into parallel tool_use
            # blocks; only the first is validated downstream, so the rest would
            # fail the step or silently drop actions.
            params["tool_choice"] = {"type": "auto", "disable_parallel_tool_use": True}
        tools = params.get("tools") or []
        output_tool = tools[0].get("name") if tools else None
        if (
            getattr(self, "_force_stream", False)
            or getattr(self, "stream_observer", None) is not None
            or (params.get("max_tokens") or 0) > 16384
        ):
            response = await self._stream_message(**params)
        else:
            response = await super()._create_message(**params)
        try:
            repair_anthropic_message(
                response,
                output_tool_name=output_tool,
                action_names=getattr(self, "_action_names", None),
                param_kinds=getattr(self, "_action_param_kinds", None),
            )
        except Exception:
            logger.debug("action-leak repair pass failed", exc_info=True)
        try:
            thinking = "\n".join(
                t
                for b in (getattr(response, "content", None) or [])
                if getattr(b, "type", None) == "thinking"
                and (t := getattr(b, "thinking", ""))
            )
            if thinking.strip():
                self._last_model_reasoning = thinking
                self._last_reasoning_seconds = getattr(self, "_reasoning_seconds", None)
        except Exception:
            logger.debug("thinking capture failed", exc_info=True)
        return response

    async def _stream_message(self, **params: Any) -> Any:
        betas = params.pop("betas", None)
        client = self.get_client()
        if betas is not None:
            async with client.beta.messages.stream(**params, betas=betas) as stream:
                return await self._drain_stream(stream)
        async with client.messages.stream(**params) as stream:
            return await self._drain_stream(stream)

    async def _drain_stream(self, stream: Any) -> Any:
        observer = getattr(self, "stream_observer", None)
        sid = getattr(self, "_activity_session", None)
        loop = asyncio.get_running_loop()
        parts: list[str] = []
        think_parts: list[str] = []
        last_push = 0.0
        began_at: float | None = None
        async for event in stream:
            etype = getattr(event, "type", "")
            if sid and etype == "content_block_start":
                block = getattr(event, "content_block", None)
                # @nonobvious(forced-by): adaptive thinking reasons privately and
                # can open a block then stay silent for tens of seconds, so the
                # label goes up on the block itself rather than the first delta.
                if getattr(block, "type", None) == "thinking":
                    if began_at is None:
                        began_at = loop.time()
                    set_activity(sid, _REASONING_LABEL, spin=True, kind="reasoning")
            if sid and etype == "content_block_delta":
                delta = getattr(event, "delta", None)
                chunk = getattr(delta, "thinking", None)
                if chunk:
                    think_parts.append(chunk)
                    now = loop.time()
                    if began_at is None:
                        began_at = now
                    if now - last_push > _REASONING_PUSH_INTERVAL_S:
                        last_push = now
                        text = " ".join("".join(think_parts).split())
                        set_activity(
                            sid,
                            _REASONING_LABEL,
                            spin=True,
                            stream=text[:_REASONING_MAX_CHARS],
                            kind="reasoning",
                        )
            if observer is None:
                continue
            if etype == "input_json" and getattr(event, "partial_json", ""):
                parts.append(event.partial_json)
                await observer.on_partial("".join(parts))
        self._reasoning_seconds = (
            round(loop.time() - began_at, 1) if began_at is not None else None
        )
        return await stream.get_final_message()

    async def ainvoke(self, messages: Any, output_format: Any = None, **kwargs: Any) -> Any:
        result = await self._ainvoke_inner(messages, output_format, **kwargs)
        await _settle_code_stream(self, result, output_format)
        thinking_text = " ".join((getattr(result, "thinking", None) or "").split())
        if thinking_text:
            self._last_model_reasoning = thinking_text
            self._last_reasoning_seconds = getattr(self, "_reasoning_seconds", None)
            sid = getattr(self, "_activity_session", None)
            if sid:
                set_activity(
                    sid,
                    _REASONING_LABEL,
                    stream=thinking_text[:_REASONING_MAX_CHARS],
                    seconds=self._last_reasoning_seconds,
                    kind="reasoning",
                )
        return result

    async def _ainvoke_inner(
        self, messages: Any, output_format: Any = None, **kwargs: Any
    ) -> Any:
        sid = getattr(self, "_activity_session", None)
        if sid:
            last = getattr(self, "_last_action", None)
            label = "Model reasoning" + (f" · next step after {last}" if last else "")
            set_activity(sid, label, spin=True, kind="reasoning")
        async def _call(msgs: Any) -> Any:
            guard = getattr(self, "_budget_guard", None)
            if guard is not None:
                guard.precheck(msgs, getattr(self, "max_tokens", None))
            result = await super(_RepairingChatAnthropic, self).ainvoke(
                msgs, output_format, **kwargs
            )
            if guard is not None:
                guard.postcheck(getattr(result, "usage", None))
            return result

        try:
            try:
                return await _invoke_with_action_repair(_call, messages, output_format)
            except ModelOutputTruncatedError:
                logger.info("Output truncated; retrying with streaming + max_tokens=64000")
                prev_mt, prev_to = self.max_tokens, self.timeout
                self._force_stream = True
                self.max_tokens = 64000
                self.timeout = 600
                try:
                    return await _call(messages)
                finally:
                    self.max_tokens = prev_mt
                    self.timeout = prev_to
                    self._force_stream = False
        finally:
            if sid:
                set_activity(sid, "Running actions", spin=True)


_ANTHROPIC_MODELS: tuple[str, ...] = (
    "claude-fable-5",
    "claude-mythos-5",
    "claude-sonnet-5",
    "claude-sonnet-4-6",
    "claude-opus-5",
    "claude-opus-4-8",
    "claude-opus-4-7",
    "claude-opus-4-6",
)

_OPENAI_MODELS: tuple[str, ...] = (
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
)


# @nonobvious(means): providers disagree on version punctuation
# (claude-sonnet-4-6 against gpt-5.6-terra), so either spelling of a version
# number resolves, and the canonical wire id is what comes back out.
_VERSION_DOT = re.compile(r"(?<=\d)\.(?=\d)")


def _lookup_key(model: str) -> str:
    return _VERSION_DOT.sub("-", model)


_MODELS_BY_KEY: dict[str, tuple[str, str]] = {
    _lookup_key(model_id): (provider, model_id)
    for provider, model_ids in (
        ("anthropic", _ANTHROPIC_MODELS),
        ("openai", _OPENAI_MODELS),
    )
    for model_id in model_ids
}

_THINKING_BUDGETS: dict[str, int] = {
    "low": 2048,
    "medium": 8192,
    "high": 16384,
}

# @nonobvious(forced-by): OpenAI counts reasoning tokens inside the output
# budget; "default" gets the medium tier because omitted ≈ medium.
_OPENAI_REASONING_HEADROOM: dict[str, int] = {
    "default": 8192,
    "low": 4096,
    "medium": 8192,
    "high": 12288,
    "xhigh": 20480,
    "max": 28672,
}

_OPENAI_LONG_EFFORTS = ("high", "xhigh", "max")

_FULL_LADDER = ("low", "medium", "high", "xhigh", "max")


@dataclass(frozen=True)
class ModelReasoning:
    """One model's provider-side reasoning capability (model reasoning), as
    distinct from the always-on step reasoning in the feed (browser thinking).

    ``default`` is what an unset effort means for this model: "none" or a
    level name.
    """

    efforts: tuple[str, ...]
    default: str
    can_disable: bool
    style: str  # @nonobvious(means): "adaptive" | "budget" | "openai-responses" wire shape

# @nonobvious(must-hold): rows mirror the live APIs as probed 2026-08-16:
# Fable/Mythos 400 on a disabled config; the Responses endpoint accepts "none"
# through "max" (chat.completions rejects "max"). Re-probe before editing.
_MODEL_REASONING: dict[str, ModelReasoning] = {
    "claude-sonnet-5": ModelReasoning(_FULL_LADDER, "high", True, "adaptive"),
    "claude-opus-5": ModelReasoning(_FULL_LADDER, "high", True, "adaptive"),
    "claude-fable-5": ModelReasoning(_FULL_LADDER, "high", False, "adaptive"),
    "claude-mythos-5": ModelReasoning(_FULL_LADDER, "high", False, "adaptive"),
    "claude-opus-4-8": ModelReasoning(_FULL_LADDER, "none", True, "adaptive"),
    "claude-opus-4-7": ModelReasoning(("low", "medium", "high"), "none", True, "budget"),
    "claude-opus-4-6": ModelReasoning(("low", "medium", "high"), "none", True, "budget"),
    "claude-sonnet-4-6": ModelReasoning(("low", "medium", "high"), "none", True, "budget"),
    "gpt-5.6-terra": ModelReasoning(_FULL_LADDER, "medium", True, "openai-responses"),
    "gpt-5.6-sol": ModelReasoning(_FULL_LADDER, "medium", True, "openai-responses"),
    "gpt-5.6-luna": ModelReasoning(_FULL_LADDER, "medium", True, "openai-responses"),
}


def model_reasoning(model: str) -> ModelReasoning:
    _, model_id = _resolve_model(model)
    return _MODEL_REASONING[model_id]


def valid_efforts(model: str) -> list[str]:
    spec = model_reasoning(model)
    out = ["default"]
    if spec.can_disable:
        out.append("none")
    out.extend(spec.efforts)
    return out


# @nonobvious(mirrors): the benchmark-backed picks from the README's
# recommended-models section. Kept here rather than in the dashboard because the
# API resolves an omitted reasoningEffort through the same table, so the same
# request run through either door reasons the same way.
_RECOMMENDED_EFFORT: dict[str, str] = {
    "claude-sonnet-5": "high",
    "claude-opus-5": "medium",
    "gpt-5.6-terra": "none",
    "gpt-5.6-sol": "none",
    "gpt-5.6-luna": "max",
}


def resolve_default_effort(model: str) -> str:
    """What the provider does when no effort is sent. Not what we send."""
    return model_reasoning(model).default


def recommended_effort(model: str) -> str | None:
    _, model_id = _resolve_model(model)
    return _RECOMMENDED_EFFORT.get(model_id)


def effort_when_unset(model: str) -> str:
    """The effort a request that named none actually runs at."""
    return recommended_effort(model) or resolve_default_effort(model)


def validate_effort(model: str, effort: str | None) -> str:
    """The canonical reasoningEffort for this model, or a loud ValueError."""
    key = (effort or "default").strip().lower()
    allowed = valid_efforts(model)
    if key not in allowed:
        if key == "none" and not model_reasoning(model).can_disable:
            raise ValueError(
                f"reasoning cannot be disabled on {model}. "
                f"Valid values: {', '.join(allowed)}."
            )
        raise ValueError(
            f"'{effort}' is not a valid reasoning effort for {model}. "
            f"Valid values: {', '.join(allowed)}."
        )
    return key


def _canonical_stored_effort(value: str | None) -> str:
    effort = (value or "default").strip().lower()
    # @nonobvious(mirrors): pre-rename session rows stored "off" for "none".
    return "none" if effort == "off" else effort


def _resolve_model(model: str) -> tuple[str, str]:
    key = (model or "").strip()
    if key.endswith("[1m]"):
        key = key[:-4]
    resolved = _MODELS_BY_KEY.get(_lookup_key(key))
    if resolved is None:
        raise ValueError(f"'{key}' is not a valid model.")
    return resolved


def _build_llm(model: str, reasoning_effort: str | None) -> tuple[str, str, Any]:
    want_1m = (model or "").strip().endswith("[1m]")
    provider, model_id = _resolve_model(model)
    effort = validate_effort(model, reasoning_effort)
    spec = _MODEL_REASONING[model_id]
    if provider == "openai":
        if not settings.openai_api_key:
            raise ValueError(f"Model '{model}' needs OPENAI_API_KEY, which is not configured")
        completion_budget = 4096 + _OPENAI_REASONING_HEADROOM.get(effort, 0)
        llm = _ResponsesChatOpenAI(
            model=model_id,
            api_key=settings.openai_api_key,
            reasoning_effort=None if effort == "default" else effort,
            max_completion_tokens=completion_budget,
            timeout=240 if effort in _OPENAI_LONG_EFFORTS else 90,
            max_retries=3,
            # @nonobvious(forced-by): OpenAI strict structured output forbids the
            # free-form dicts our action registry needs, so the schema rides in
            # the system prompt and the reply is parsed tolerantly.
            add_schema_to_system_prompt=True,
            dont_force_structured_output=True,
        )
        return provider, model_id, llm

    if not settings.anthropic_api_key:
        raise ValueError(f"Model '{model}' needs ANTHROPIC_API_KEY, which is not configured")
    kwargs: dict[str, Any] = {
        "model": model_id,
        "api_key": settings.anthropic_api_key,
        "timeout": 180,
        "max_retries": 3,
        "max_tokens": 16384,
    }
    if want_1m:
        kwargs["betas"] = [ONE_M_BETA]
    # @nonobvious(forced-by): omitting `thinking` means adaptive ON at high on
    # Claude 5, so "none" must send an explicit disabled config, never omit.
    if effort == "none":
        kwargs["thinking"] = {"type": "disabled"}
    elif effort != "default":
        if spec.style == "adaptive":
            kwargs["thinking"] = {"type": "adaptive", "display": "summarized"}
            kwargs["output_config"] = {"effort": effort}
        else:
            budget = _THINKING_BUDGETS[effort]
            kwargs["thinking"] = {"type": "enabled", "budget_tokens": budget}
            kwargs["max_tokens"] = max(budget + 8192, 16384)
    return provider, model_id, _RepairingChatAnthropic(**kwargs)


_storage_locks: dict[str, asyncio.Lock] = {}


def _storage_lock(path: str) -> asyncio.Lock:
    lock = _storage_locks.get(path)
    if lock is None:
        lock = asyncio.Lock()
        _storage_locks[path] = lock
    return lock


def _strip_json_fence(text: str) -> str:
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[-1] if "\n" in t else ""
        if t.endswith("```"):
            t = t[:-3]
    return t.strip()


async def _coerce_to_schema(output: Any, model: type, llm: Any) -> tuple[str, bool]:
    """Validate ``output`` against ``model``; on failure ask the LLM once to
    reformat it. Returns ``(json_string, schema_valid)``.
    """
    if not output:
        return output, False
    try:
        obj = (
            model.model_validate_json(output)
            if isinstance(output, str)
            else model.model_validate(output)
        )
        return obj.model_dump_json(), True
    except Exception:
        pass
    try:
        schema_json = json.dumps(model.model_json_schema())
        prompt = (
            "Convert the following result into JSON that strictly conforms to this "
            "JSON Schema. Output only the JSON, with no prose or explanation.\n\n"
            f"Schema:\n{schema_json}\n\nResult:\n{output}"
        )
        resp = await llm.ainvoke([UserMessage(content=prompt)])
        text = getattr(resp, "completion", None) or str(resp)
        obj = model.model_validate_json(_strip_json_fence(text))
        return obj.model_dump_json(), True
    except Exception:
        logger.warning("Output failed schema validation and reformat pass failed", exc_info=True)
        return (output if isinstance(output, str) else json.dumps(output)), False


def _validate_only(output: Any, model: type) -> bool:
    """Validate against the model with NO LLM reformat — the store is already per-item
    validated, so a top-level miss must never trigger a rewrite that could fabricate.
    """
    try:
        if isinstance(output, str):
            model.model_validate_json(output)
        else:
            model.model_validate(output)
        return True
    except Exception:
        return False


_CODE_ACTIONS = ("evaluate", "find_", "search_page")


def _action_detail(actions: list) -> tuple[str, bool]:
    """The primary action's key parameter for the feed summary — without the action
    name (the feed chip already shows that) — plus whether it should render as code
    (a CSS selector or a JS snippet reads better in monospace than as prose).
    """
    name = _primary_action_name(actions)
    if not actions or not name:
        return "", False
    try:
        params = actions[0].model_dump(exclude_none=True).get(name)
    except Exception:
        params = None

    if name == "remember" and isinstance(params, dict):
        return (f"{params.get('key', '')} = {params.get('value', '')}"[:200], False)
    if name == "open_tabs" and isinstance(params, dict):
        return (f"{len(params.get('urls', []))} tabs", False)
    if name == "goto_tab" and isinstance(params, dict):
        return (str(params.get("n")), False)
    if name == "open_in_new_tab" and isinstance(params, dict):
        return (f"index {params.get('index')}", False)
    if name == "close_tab":
        return ("current tab", False)
    if name == "find_links" and isinstance(params, dict):
        for pk in ("href_contains", "href_regex", "frame_url_contains", "container_index", "attr"):
            v = params.get(pk)
            if v not in (None, ""):
                return (f"{pk}={v}"[:200], False)
        return ("", False)
    if name == "read_pages" and isinstance(params, dict):
        urls = params.get("urls") or []
        frame = params.get("frame_url_contains")
        base = (
            f"Read {len(urls)} pages in parallel"
            if urls
            else "Read every page from the saved link set"
        )
        return ((f"{base} (frame: {frame})" if frame else base)[:200], False)
    if name == "update_items" and isinstance(params, dict):
        return (f"Update {len(params.get('updates') or [])} item(s) in the output", False)
    if name == "mark_absent" and isinstance(params, dict):
        return (str(params.get("field", ""))[:200], False)
    if name == "run_code_file" and isinstance(params, dict):
        url = params.get("url")
        base = str(params.get("name", ""))
        return ((f"{base} @ {url}" if url else base)[:200], False)
    if name in ("read_file", "add_items_from_file", "update_items_from_file") and isinstance(
        params, dict
    ):
        fname = str(params.get("file_name") or params.get("name") or "")
        verb = {
            "read_file": "Read saved file",
            "add_items_from_file": "Add output rows from",
            "update_items_from_file": "Update output rows from",
        }[name]
        return (f"{verb} {fname}".strip()[:200], False)
    if name == "read_output":
        return ("Check the output built so far", False)

    detail = ""
    if isinstance(params, dict):
        for pk in ("url", "selector", "query", "text", "code", "expression", "keys", "index", "seconds"):
            value = params.get(pk)
            if value not in (None, ""):
                val = str(value)
                detail = val[:500] + "…" if len(val) > 500 else val
                break
    is_code = bool(detail) and any(k in name.lower() for k in _CODE_ACTIONS)
    return detail, is_code


def _reasoned_title(seconds: float | None) -> str:
    """The reasoning row is a headline, and half a sentence of thought is not
    one; the whole thought is a caret away in the card.
    """
    if not seconds:
        return "Reasoned"
    if seconds >= 60:
        return f"Reasoned for {int(seconds // 60)}m {round(seconds % 60)}s"
    # @nonobvious(mirrors): one decimal, matching the duration badge the row
    # already shows beside it, so the two do not disagree by a second.
    return f"Reasoned for {round(seconds, 1)}s"


def _friendly_error(error: str) -> str:
    """First sentence of an error, clipped — the row is a headline, and the
    step card carries the full text for anyone who expands it.
    """
    text = " ".join((error or "").split())
    for stop in (". ", "; "):
        cut = text.find(stop)
        if 0 < cut < 140:
            return text[: cut + 1] + "…"
    return text[:140] + ("…" if len(text) > 140 else "")


def _gated_done_output(history: Any) -> str:
    """history.final_result() returns the last action's extracted_content
    regardless of whether done() was ever reached, so an unfinished run must
    not have its last step mistaken for delivered output."""
    if not history.is_done():
        return ""
    return history.final_result() or ""


def _budget_salvage(
    agent: Any,
    store: OutputStore | None,
    clipboard: dict[str, Any],
    output_model: type | None,
) -> tuple[str, bool]:
    """Output and success verdict for a run cut short by its cost cap.

    A cap fires between steps, so the agent never reaches done, but the store may
    already hold a complete answer; the same gate the normal finish trusts decides
    whether it does.
    """
    # @nonobvious(must-hold): nothing here may call the LLM. The budget that
    # ended the run is the budget schema coercion would spend, so output that
    # does not already validate is kept as it stands and recorded as a failure.
    output = ""
    if store is not None and not store.is_empty():
        output = store.read_output()
    else:
        try:
            result_file = agent.file_system.get_file("result.json") if agent.file_system else None
            if result_file:
                text = result_file.read()
                if text and text.strip():
                    output = text
        except Exception:
            logger.debug("result.json read during budget salvage failed", exc_info=True)

    if not output or store is None or output_model is None:
        return output, False
    if not _validate_only(output, output_model):
        return output, False
    return output, not _gate_empty_fields(store, clipboard)


def _completion_summary(
    *,
    is_successful: bool,
    is_done: bool,
    raw_success: bool,
    schema_valid: bool,
    stopped: bool,
    done_text: str,
    recovered_errors: int,
) -> str:
    """One honest sentence for the ending, distinguishing an agent-reported
    failure from a user stop and from running out of steps — the three were
    previously collapsed into a single "Task finished with errors"."""
    if is_successful:
        summary = "Task completed successfully"
        if recovered_errors:
            plural = "s" if recovered_errors != 1 else ""
            summary += f" (recovered from {recovered_errors} transient error{plural})"
        return summary
    if is_done and raw_success and not schema_valid:
        return "Task finished but the result did not match the requested schema"
    if is_done and not raw_success:
        reason = f": {_friendly_error(done_text)}" if done_text else ""
        return f"Task failed{reason}"
    if stopped:
        return "Task failed: stopped before the goal was reached"
    return "Task failed: ran out of steps before the goal was reached"


def _primary_action_name(actions: list) -> str | None:
    if not actions:
        return None
    try:
        dumped = actions[0].model_dump(exclude_none=True)
    except Exception:
        return None
    return next(iter(dumped), None) if dumped else None


def _executed_actions(
    model_output: Any, results: list | None
) -> tuple[list[str], list[str], list[str], str | None]:
    """(requested names, executed names, executed argument fingerprints, first
    erroring action).

    @nonobvious(must-hold): a step's requested action chain is cut short on error,
    on a sequence-terminating action, or on a page change, and only actions that
    ran get a result. Anything past the executed slice was requested but never
    performed and must not be recorded as having happened — assertions about which
    tools a run really used depend on that.
    """
    names: list[str] = []
    args: list[str] = []
    actions = getattr(model_output, "action", None) or []
    for act in actions:
        try:
            dumped = act.model_dump(exclude_none=True)
        except Exception:
            continue
        names.extend(dumped.keys())
        args.extend(
            guard_key(json.dumps(params, sort_keys=True, default=str))
            for params in dumped.values()
        )
    executed_n = len(results or [])
    error_action: str | None = None
    for i, result in enumerate(results or []):
        if getattr(result, "error", None):
            if i < len(names):
                error_action = names[i]
            break
    return names, names[:executed_n], args[:executed_n], error_action


def _category_for(action_name: str | None) -> str:
    n = (action_name or "").lower()
    if any(k in n for k in ("add_item", "update_item", "set_field", "read_output", "search_output", "mark_absent")):
        return "schema"
    if "read_pages" in n:
        return "read"
    if any(k in n for k in ("navigate", "go_to", "go_back", "search", "switch")):
        return "navigation"
    if any(k in n for k in ("click", "input", "scroll", "send_keys", "select", "dropdown", "upload", "type")):
        return "interaction"
    if any(k in n for k in ("evaluate", "python", "execute_js", "code_file")):
        return "code"
    if "fetch" in n:
        return "network"
    if any(k in n for k in ("extract", "find_", "search_page", "get_html", "screenshot", "pdf")):
        return "read"
    if "wait" in n:
        return "wait"
    if "captcha" in n:
        return "interaction"
    if "done" in n:
        return "done"
    return "action"


async def _derive_north_star(llm: Any, task: str) -> str:
    """One short thinking-off LLM call naming the task's North Star, so the goal can
    be pinned under the task and re-injected periodically. Falls back to the task's
    first sentence if the call fails.
    """
    try:
        resp = await llm.ainvoke(
            [UserMessage(content=f"{_GOAL_PROMPT}\n\nTASK:\n{task}")]
        )
        text = " ".join((getattr(resp, "completion", None) or str(resp)).split())
        if text:
            return text[:400]
    except Exception:
        logger.debug("North Star pre-flight failed", exc_info=True)
    first = re.split(r"(?<=[.!?])\s", (task or "").strip(), maxsplit=1)[0]
    return first.strip()[:400] or (task or "").strip()[:400]


# @nonobvious(forced-by): models emit JSON in schema order and drop trailing
# properties under pressure — "action" must sit early, prose cards last.
_CARD_ORDER = (
    "thinking", "action", "what_i_see", "plan_to_goal", "next_move",
    "evaluation_previous_goal", "memory", "next_goal",
    "current_plan_item", "plan_update",
)
_cards_patched = False


def _patch_agent_output_cards() -> None:
    """Add three purpose-built one-sentence fields (what_i_see / plan_to_goal /
    next_move) to browser-use's per-step AgentOutput by wrapping the factory
    staticmethods it rebuilds the model from every step (``_update_action_models_for_page``).
    Fields are required in the emitted schema (so the model fills them) but optional on
    the model (so an omission never fails validation). Guarded + best-effort: on any
    incompatibility we log and fall back to browser-use's built-in fields.
    """
    global _cards_patched
    if _cards_patched:
        return
    try:
        from browser_use.agent.views import AgentOutput
        from pydantic import Field

        def _wrap(orig):
            def factory(custom_actions):
                base = orig(custom_actions)

                class CardedAgentOutput(base):  # type: ignore[misc, valid-type]
                    what_i_see: str | None = Field(
                        None, description="One sentence: what you can see on the page right now."
                    )
                    plan_to_goal: str | None = Field(
                        None, description="One sentence: how you get from here to the goal."
                    )
                    next_move: str | None = Field(
                        None, description="One sentence: your next single move."
                    )

                    @classmethod
                    def model_json_schema(cls, **kwargs):
                        schema = super().model_json_schema(**kwargs)
                        props = schema.get("properties", {})
                        ordered = {k: props[k] for k in _CARD_ORDER if k in props}
                        for k, v in props.items():
                            ordered.setdefault(k, v)
                        schema["properties"] = ordered
                        # @nonobvious(forced-by): required-cards under forced tool_choice made Claude bleed XML into JSON values.
                        return schema

                CardedAgentOutput.__name__ = "AgentOutput"
                return CardedAgentOutput

            return factory

        AgentOutput.type_with_custom_actions = staticmethod(
            _wrap(AgentOutput.type_with_custom_actions)
        )
        AgentOutput.type_with_custom_actions_no_thinking = staticmethod(
            _wrap(AgentOutput.type_with_custom_actions_no_thinking)
        )
        _cards_patched = True
    except Exception:
        logger.warning(
            "AgentOutput card patch failed; falling back to built-in fields", exc_info=True
        )


_patch_agent_output_cards()


_CONTINUATION_PREFIX = (
    "This is a follow-up in the same session. The browser is exactly as you left "
    "it — same tabs, same page, same logins and cookie choices — and everything "
    "you learned above still holds. Look at where you already are before you "
    "navigate, and only open a new page if this request genuinely needs one. "
    "Resolve any names or pronouns below from the conversation so far.\n\n"
)

_TURN_OUTPUT_CAP = 4000

# @nonobvious(forced-by): browser-use counts steps for the life of the agent and
# stops at max_steps, so a keep-alive session that has already taken 500 steps
# would see later follow-ups return instantly having done nothing. Each turn gets
# its own allowance on top of what the session has already spent.
_TURN_STEP_BUDGET = 500


def _turn_step_cap(agent: Any) -> int:
    taken = getattr(getattr(agent, "state", None), "n_steps", None)
    if not isinstance(taken, int):
        taken = len(getattr(getattr(agent, "history", None), "history", []) or [])
    return taken + _TURN_STEP_BUDGET


def _north_star_preflight(requested_model: str, text: str) -> asyncio.Task | None:
    """Start the one-line North Star call for a turn, so it overlaps with the
    browser launch (first turn) or with the agent waking up (a follow-up).
    """
    try:
        preflight_effort = "none" if model_reasoning(requested_model).can_disable else "default"
        _, _, preflight_llm = _build_llm(requested_model, preflight_effort)
        try:
            preflight_llm.max_tokens = 300
        except Exception:
            pass
        return asyncio.create_task(_derive_north_star(preflight_llm, text))
    except Exception:
        logger.debug("North Star pre-flight setup failed", exc_info=True)
        return None


async def _prepare_task(
    *,
    session_id: str,
    task: str,
    url_text: str,
    clipboard: dict[str, Any],
    review_state: dict[str, Any],
    preflight: "asyncio.Task | None",
    output_schema: dict[str, Any] | None,
    output_model: type | None,
    preamble: str = "",
) -> str:
    """What one turn needs before the agent runs: its North Star, a clean review
    state, the goal and startUrl rows, and the text the agent is actually given.
    """
    review_state["round"] = 0
    review_state["snapshot"] = None

    north_star = ""
    if preflight is not None:
        try:
            north_star = await preflight
        except Exception:
            logger.debug("North Star pre-flight await failed", exc_info=True)
    if not north_star:
        north_star = re.split(r"(?<=[.!?])\s", (task or "").strip(), maxsplit=1)[0][:400]
    clipboard["northStar"] = north_star

    full_task = f"{preamble}{task}" if preamble else task
    if north_star:
        full_task = f"{full_task}\n\nGOAL: {north_star}"
        await crud.create_message(
            session_id=session_id,
            role="ai",
            msg_type="event",
            data=json.dumps({"category": "goal", "action": "goal"}),
            summary=north_star,
            count_step=False,
        )
    if output_schema and output_model is None:
        schema_str = json.dumps(output_schema, indent=2)
        full_task = (
            f"{full_task}\n\n"
            f"OUTPUT FORMAT: Return your result as JSON conforming to this schema:\n"
            f"```json\n{schema_str}\n```"
        )

    start_match = re.search(r"https?://[^\s\"'<>)\]]+", url_text or "")
    if start_match:
        start_url = start_match.group(0).rstrip(".,;)")
        clipboard["startUrl"] = start_url
        await crud.create_message(
            session_id=session_id,
            role="ai",
            msg_type="event",
            data=json.dumps({"category": "memory", "action": "startUrl"}),
            summary=f"startUrl: {start_url}",
            count_step=False,
        )
    return full_task


async def _finalise_task(
    *,
    session_id: str,
    agent: Any,
    history: Any,
    llm: Any,
    store: "OutputStore | None",
    output_model: type | None,
    output_schema: dict[str, Any] | None,
    clipboard: dict[str, Any],
    capsolver_costs: list[float],
    carried: dict[str, float],
    steps_before: int,
    final_status: str | None,
) -> None:
    """Record one finished turn: its answer, whether it succeeded, the session's
    running cost, and the completion row. ``steps_before`` is where this turn
    started in the agent's history, so a follow-up is not blamed for the errors
    of the turn before it. ``carried`` is the spend and token count the session
    had already recorded before this worker began, so the totals written here are
    the session's rather than this worker's. ``final_status`` is left None by a
    keep-alive session, whose row only turns 'idle' once its worker is genuinely
    parked and able to take the next follow-up.
    """
    file_output = ""
    try:
        result_file = agent.file_system.get_file("result.json") if agent.file_system else None
        if result_file:
            file_content = result_file.read()
            if file_content and file_content.strip():
                file_output = file_content
    except Exception:
        logger.debug("result.json read from agent.file_system failed", exc_info=True)
    done_output = _gated_done_output(history)
    from_store = store is not None and not store.is_empty()
    if from_store:
        output = store.read_output()
    else:
        output = done_output or file_output

    schema_valid = True
    if output_model is not None and from_store:
        schema_valid = _validate_only(output, output_model)
    elif output_model is not None:
        output, schema_valid = await _coerce_to_schema(output, output_model, llm)
        if not schema_valid and done_output and file_output and file_output != done_output:
            alt, alt_valid = await _coerce_to_schema(file_output, output_model, llm)
            if alt_valid:
                output, schema_valid = alt, alt_valid
    elif output_schema and output:
        try:
            parsed = json.loads(output) if isinstance(output, str) else output
            output = json.dumps(parsed)
        except (json.JSONDecodeError, TypeError):
            pass

    recovered_errors = sum(1 for e in history.errors()[steps_before:] if e)
    raw_success = history.is_successful() is not False
    is_successful = history.is_done() and raw_success and schema_valid
    # @nonobvious(means): a run that died before done but left a complete,
    # valid store has delivered the answer; the gate check is the arbiter.
    if not is_successful and not history.is_done() and from_store and schema_valid:
        try:
            if store is not None and not _gate_empty_fields(store, clipboard):
                is_successful = True
                await crud.create_message(
                    session_id=session_id,
                    role="ai",
                    msg_type="event",
                    summary=(
                        "Run ended without done, but the output store is "
                        "complete and schema-valid — recorded as success"
                    ),
                    data=json.dumps({"category": "judge", "action": "storeComplete"}),
                    count_step=False,
                )
        except Exception:
            logger.debug("store-complete success check failed", exc_info=True)

    usage_history = agent.token_cost_service.usage_history
    llm_cost = carried["llm"] + cost.history_cost(usage_history)
    capsolver_cost = carried["capsolver"] + sum(capsolver_costs)
    total_cost = llm_cost + capsolver_cost
    total_input = int(
        carried["input_tokens"]
        + sum((u.usage.prompt_tokens or 0) for u in usage_history if u.usage)
    )
    total_output = int(
        carried["output_tokens"]
        + sum((u.usage.completion_tokens or 0) for u in usage_history if u.usage)
    )

    status_update = {"status": final_status} if final_status else {}
    await crud.update_session(
        session_id,
        **status_update,
        output=output,
        is_task_successful=int(is_successful),
        failure_kind=None,
        failure_status_code=None,
        total_input_tokens=total_input,
        total_output_tokens=total_output,
        llm_cost_usd=llm_cost,
        capsolver_cost_usd=capsolver_cost,
        total_cost_usd=total_cost,
    )

    judgement = _last_judgement(history)
    if judgement is not None and bool(judgement.verdict) != bool(is_successful):
        judge_word = "PASS" if judgement.verdict else "FAIL"
        own_word = "success" if is_successful else "failure"
        reason = " ".join(
            (judgement.failure_reason or judgement.reasoning or "").split()
        )[:_REVIEW_REASON_CAP]
        await crud.create_message(
            session_id=session_id,
            role="ai",
            msg_type="event",
            summary=(
                reason
                or f"review outcome {judge_word} differs from recorded {own_word}"
            ),
            data=json.dumps(
                {
                    "category": "judge",
                    "action": "review",
                    "verdict": judge_word,
                    "recorded": own_word,
                }
            ),
            count_step=False,
        )

    completion_summary = _completion_summary(
        is_successful=is_successful,
        is_done=history.is_done(),
        raw_success=raw_success,
        schema_valid=schema_valid,
        stopped=bool(getattr(getattr(agent, "state", None), "stopped", False)),
        done_text=done_output,
        recovered_errors=recovered_errors,
    )
    # @nonobvious(means): the answer rides along on the completion row (the
    # feed renders only the summary) so a later follow-up can be replayed with
    # what was actually said, not just that a turn happened.
    await crud.create_message(
        session_id=session_id,
        role="ai",
        msg_type="completion",
        summary=completion_summary,
        data=json.dumps({"output": (output or "")[:_TURN_OUTPUT_CAP]}),
    )


async def _wait_for_followup(entry: live.LiveSession, idle_timeout: int) -> str | None:
    """Park until the next follow-up arrives, or until this session is released —
    by a stop, by a new session claiming its display slot, or by sitting idle for
    ``idle_timeout`` seconds (0 waits forever). None means: tear down.
    """
    inbox = asyncio.ensure_future(entry.inbox.get())
    released = asyncio.ensure_future(entry.release.wait())
    try:
        done, _ = await asyncio.wait(
            {inbox, released},
            timeout=idle_timeout or None,
            return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        for pending in (inbox, released):
            if not pending.done():
                pending.cancel()
    if inbox in done and released not in done:
        return inbox.result()
    if inbox in done:
        # @nonobvious(must-hold): a follow-up that landed in the same moment as
        # the release is put back, so the teardown can tell the user it never ran
        # instead of dropping it silently.
        entry.inbox.put_nowait(inbox.result())
    return None


async def _record_release(session_id: str, entry: live.LiveSession) -> None:
    """Close out a keep-alive session: say why the browser went away and move the
    row off 'idle', which is what offers a follow-up box for a parked browser.
    """
    if entry.release.is_set():
        note = entry.release_reason or "Keep-alive session released"
    else:
        minutes = max(1, round(settings.keep_alive_idle_timeout / 60))
        plural = "" if minutes == 1 else "s"
        note = f"Expired: no follow-up for {minutes} idle minute{plural}"
    if not entry.inbox.empty():
        note += (
            " — a follow-up arrived as it was closing and did not run; send it "
            "again to continue in a fresh browser"
        )
    await crud.create_message(
        session_id=session_id,
        role="ai",
        msg_type="event",
        data=json.dumps({"category": "system", "action": "keepAlive"}),
        summary=note,
        count_step=False,
    )
    await crud.update_session(session_id, status="stopped")


async def run_agent_session(session_id: str) -> None:
    """Execute a browser-use agent for the given session. Runs as a background task.

    A keep-alive session does not end when its task does: the worker parks with
    Chrome and the agent still alive and runs each follow-up on that same agent,
    so the conversation, the open tabs and the running cost all continue. It lets
    go when the session is stopped, when a new session claims its display slot, or
    after ``KEEP_ALIVE_IDLE_TIMEOUT`` seconds without a follow-up.
    """
    session = await crud.get_session(session_id)
    if not session:
        logger.error("Session %s not found", session_id)
        return

    task = session.get("task")
    if not task:
        await crud.update_session(
            session_id,
            status="error",
            failure_kind="agent_failure",
            failure_status_code=None,
        )
        return

    keep_alive = bool(session.get("keep_alive"))
    requested_model = session.get("model") or settings.resolved_default_model
    reasoning_effort = _canonical_stored_effort(session.get("reasoning_effort"))
    output_schema = json.loads(session["output_schema"]) if session.get("output_schema") else None
    sensitive_data = json.loads(session["sensitive_data"]) if session.get("sensitive_data") else None
    system_prompt_extension = session.get("system_prompt_extension")

    try:
        provider, model, llm = _build_llm(requested_model, reasoning_effort)
    except ValueError as e:
        logger.error("Session %s LLM setup failed: %s", session_id, e)
        await crud.update_session(
            session_id,
            status="error",
            failure_kind="agent_failure",
            failure_status_code=None,
        )
        await crud.create_message(
            session_id=session_id,
            role="ai",
            msg_type="browser_action_error",
            summary=str(e)[:200],
        )
        return

    llm._activity_session = session_id

    # @nonobvious(means): a keep-alive session outlives its browser — the idle
    # timeout, an eviction or a restart all release Chrome while the conversation
    # stays open — so a follow-up landing here cold carries the earlier turns in
    # its prompt. Empty for a session that has not spoken yet.
    replayed = await live.replay_preamble(session_id, task) if keep_alive else ""
    north_star_task = _north_star_preflight(requested_model, replayed or task)

    # Load profile storage state path
    storage_state_path: str | None = None
    claimed_profile_id: str | None = None
    profile = None
    if session.get("profile_id"):
        profile = await crud.get_profile(session["profile_id"])
        if profile and profile.get("storage_state_path"):
            state_file = settings.data_dir / profile["storage_state_path"]
            if state_file.exists():
                storage_state_path = str(state_file)
            await crud.update_profile(
                profile["id"],
                last_used_at=datetime.now(timezone.utc).isoformat(),
            )

    if profile:
        holder = try_claim_profile(profile["id"], session_id)
        if holder is not None:
            if north_star_task is not None and not north_star_task.done():
                north_star_task.cancel()
            await crud.update_session(session_id, status="error")
            await crud.create_message(
                session_id=session_id,
                role="ai",
                msg_type="browser_action_error",
                summary=(
                    f"Profile is in use by running session {holder} — two "
                    "concurrent sessions on one profile would overwrite each "
                    "other's cookies. Retry when that session finishes."
                ),
            )
            return
        claimed_profile_id = profile["id"]

    session_started(session_id)
    slot = None
    browser_session = None
    entry: live.LiveSession | None = None
    try:
        slot = await display_manager.allocate()
        cdp_url = await launch_chrome(slot)

        live_url = f"/vnc/{session_id}/view?path=vnc/{session_id}/websockify"
        await crud.update_session(
            session_id,
            status="running",
            display_num=slot.display_num,
            live_url=live_url,
            # @nonobvious(must-hold): a follow-up run must not rename the
            # session after the thing it happens to ask last.
            title=(session.get("title") or (task[:80] if task else None)),
        )
        await crud.create_message(
            session_id=session_id,
            role="ai",
            msg_type="planning",
            summary=f"Session started with model {model}",
        )
        if replayed:
            await crud.create_message(
                session_id=session_id,
                role="ai",
                msg_type="event",
                data=json.dumps({"category": "system", "action": "keepAlive"}),
                summary=(
                    "The browser from the earlier turns was already released, so "
                    "this follow-up starts a fresh one with the conversation so "
                    "far replayed into it"
                ),
                count_step=False,
            )
        browser_session = BrowserSession(
            cdp_url=cdp_url,
            storage_state=storage_state_path,
            cross_origin_iframes=True,
        )
        # @nonobvious(forced-by): agent.run() kills the browser at run end
        # unless the profile says keep_alive, because the review loop re-runs the
        # agent, and a reviewer round against a dead browser silently re-judges
        # the same trajectory. Chrome's real teardown is ours (stop_chrome).
        browser_session.browser_profile.keep_alive = True
        await browser_session.start()
        await install_captcha_bridge(browser_session)

        clipboard: dict[str, Any] = {}
        review_state: dict[str, Any] = {"round": 0, "snapshot": None}
        tab_manager = TabManager(browser_session)
        tools = Tools()

        output_model: type | None = None
        if output_schema:
            try:
                output_model = json_schema_to_pydantic(output_schema, "TaskOutput")
            except Exception as e:
                logger.warning(
                    "output_schema -> model conversion failed, using prose fallback: %s", e
                )
                output_model = None
        store: OutputStore | None = None
        if output_model is not None:
            store = OutputStore(output_model)

        async def _read_progress(label: str) -> None:
            set_activity(session_id, label, spin=True)
            action = (label.split(":", 1)[0].split() or ["read_pages"])[0]
            await crud.create_message(
                session_id=session_id,
                role="ai",
                msg_type="event",
                data=json.dumps({"category": "read", "action": action}),
                summary=label[:200],
                count_step=False,
            )

        async def _code_progress(label: str) -> None:
            set_activity(session_id, label, spin=True)
            await crud.create_message(
                session_id=session_id,
                role="ai",
                msg_type="event",
                data=json.dumps({"category": "code", "action": "script"}),
                summary=label[:200],
                count_step=False,
            )

        async def _captcha_progress(label: str) -> None:
            set_activity(session_id, label, spin=True)
            await crud.create_message(
                session_id=session_id,
                role="ai",
                msg_type="event",
                data=json.dumps({"category": "interaction", "action": "captcha"}),
                summary=label[:200],
                count_step=False,
            )

        code_observer = CodeStreamObserver(browser_session, clipboard, _code_progress)
        object.__setattr__(llm, "stream_observer", code_observer)

        register_fetch_tool(tools)
        register_code_tools(tools, clipboard, store, _code_progress)
        register_clipboard_tools(tools, clipboard)
        register_tab_tools(tools, tab_manager, clipboard, store, _read_progress)
        register_upload_path_resolution(tools)
        capsolver_costs: list[float] = []
        # @nonobvious(forced-by): the solver refuses to spend once its own sink
        # passes CAPTCHA_MAX_COST_USD, and that sink is bound at registration —
        # so each turn empties it and what it spent moves into "capsolver" here,
        # where the session's running total still counts it.
        # @nonobvious(must-hold): the row's existing totals are seeded in, not
        # started from zero. A follow-up dispatched after this session's previous
        # worker went away builds a fresh agent whose counters are empty, and
        # without the carry the session's recorded spend would rewind and its
        # budget would refill itself.
        carried: dict[str, float] = {
            "llm": float(session.get("llm_cost_usd") or 0.0),
            "capsolver": float(session.get("capsolver_cost_usd") or 0.0),
            "input_tokens": float(session.get("total_input_tokens") or 0),
            "output_tokens": float(session.get("total_output_tokens") or 0),
        }
        budget_guard = _BudgetGuard(
            getattr(llm, "model", "") or "",
            carried,
            capsolver_costs,
            session.get("max_cost_usd"),
        )
        llm._budget_guard = budget_guard
        register_captcha_tools(tools, capsolver_costs, _captcha_progress)

        if store is not None:
            register_output_store_tools(tools, store, clipboard)

            async def _on_incomplete_done(empties: list[str]) -> None:
                await crud.create_message(
                    session_id=session_id,
                    role="ai",
                    msg_type="event",
                    data=json.dumps({"category": "schema", "action": "completeness"}),
                    summary="Completeness gate: " + "; ".join(empties)[:180],
                    count_step=False,
                )

            async def _on_complete_done(coverage: str) -> None:
                await crud.create_message(
                    session_id=session_id,
                    role="ai",
                    msg_type="event",
                    data=json.dumps({"category": "schema", "action": "completeness"}),
                    summary=("Completeness check passed — " + coverage)[:200],
                    count_step=False,
                )

            register_completeness_gate(
                tools, store, _on_incomplete_done, clipboard, _on_complete_done
            )

        register_search_page_flow(tools, clipboard)
        register_find_elements_flow(tools)
        # @nonobvious(must-hold): last, so it wraps the final version of every action.
        # Registered any earlier and a later wrapper sits OUTSIDE the guard, handed a
        # result the guard may already have replaced with a back-reference.
        register_output_guard_overrides(tools)
        try:
            llm._action_param_kinds = action_param_kinds(tools)
            llm._action_names = set(tools.registry.registry.actions)
        except Exception:
            logger.debug("action param kind map build failed", exc_info=True)

        prompt = await _prepare_task(
            session_id=session_id,
            task=replayed or task,
            url_text=task,
            clipboard=clipboard,
            review_state=review_state,
            preflight=north_star_task,
            output_schema=output_schema,
            output_model=output_model,
        )
        north_star_task = None

        pressure_level, pressure_sample = system_metrics.mark_baseline()
        if pressure_level != "ok":
            await crud.create_message(
                session_id=session_id,
                role="ai",
                msg_type="event",
                data=json.dumps({"category": "system", "action": "pressure"}),
                summary=(
                    f"⚠ Host CPU {pressure_level} at launch: load "
                    f"{pressure_sample['load1']} on {pressure_sample['cores']} "
                    "cores. Timing-sensitive embed reads (frame attach, link "
                    "rewriting, consent persistence) are degraded — failures in "
                    "this run may be environmental, not site changes."
                ),
                count_step=False,
            )

        lean_flag: dict[str, bool] = {"eligible": False}
        _install_lean_state(browser_session, lean_flag)

        step_count = 0
        step_started_at: dict[str, Any] = {"t": None}
        logged_history_len = {"n": 0}

        async def on_step_start(agent_instance: Agent) -> None:
            step_started_at["t"] = datetime.now(timezone.utc)
            set_activity(session_id, "Preparing next step", spin=True)

        async def on_step_end(agent_instance: Agent) -> None:
            nonlocal step_count
            step_count += 1

            current_url = None
            try:
                current_url = await _eval_js(browser_session, "window.location.href")
            except Exception:
                pass
            if (
                current_url
                and clipboard.get("startUrl") is None
                and not str(current_url).startswith("about:")
            ):
                clipboard["startUrl"] = current_url
                await crud.create_message(
                    session_id=session_id,
                    role="ai",
                    msg_type="event",
                    data=json.dumps({"category": "memory", "action": "startUrl"}),
                    summary=f"startUrl: {current_url}",
                    count_step=False,
                )

            steps = agent_instance.history.history
            if not steps:
                return
            # @nonobvious(forced-by): on_step_end also fires for steps cancelled
            # by step_timeout, where re-reading history[-1] would double-log.
            if len(steps) == logged_history_len["n"]:
                stopped = bool(getattr(agent_instance.state, "stopped", False))
                await crud.create_message(
                    session_id=session_id,
                    role="ai",
                    msg_type="browser_action_error",
                    summary=(
                        "Cancelled by stop request"
                        if stopped
                        else "Step timed out and was cancelled before completing"
                    ),
                )
                return
            logged_history_len["n"] = len(steps)
            step = steps[-1]

            north_star = clipboard.get("northStar")
            if north_star and step_count % 10 == 0:
                try:
                    # @nonobvious(deliberately-missing): not _inject_followup_task,
                    # because the public add_new_task resets stopped/paused state and
                    # the event bus, which must not happen mid-run.
                    agent_instance._message_manager.add_new_task(
                        f"GOAL: {north_star} Not done until this is met."
                    )
                except Exception:
                    logger.debug("north star reminder injection failed", exc_info=True)
                await crud.create_message(
                    session_id=session_id,
                    role="ai",
                    msg_type="event",
                    data=json.dumps({"category": "goal", "action": "goal"}),
                    summary=f"Goal reminder (step {step_count})",
                    count_step=False,
                )

            summary = ""
            is_code = False
            msg_type = "browser_action"

            if step.model_output:
                mo = step.model_output
                if getattr(mo, "next_move", None):
                    summary = mo.next_move
                else:
                    brain = getattr(mo, "current_state", None)
                    if brain and getattr(brain, "next_goal", None):
                        summary = brain.next_goal
                    elif mo.action:
                        summary, is_code = _action_detail(mo.action)

            full_error = ""
            if step.result:
                for result in step.result:
                    if result.error:
                        msg_type = "browser_action_error"
                        summary = f"Error: {_friendly_error(result.error)}"
                        full_error = str(result.error)
                        is_code = False
                    elif result.extracted_content:
                        msg_type = "result"

            action_name = None
            category = None
            if step.model_output and step.model_output.action:
                action_name = _primary_action_name(step.model_output.action)
                category = _category_for(action_name)
                for act in step.model_output.action:
                    try:
                        note_read_action(clipboard, _primary_action_name([act]))
                    except Exception:
                        logger.debug("read-action count failed", exc_info=True)
            all_action_names, executed_actions, executed_args, error_action = (
                _executed_actions(step.model_output, step.result)
            )
            lean_flag["eligible"] = bool(all_action_names) and all(
                n in _STORE_ONLY_ACTIONS for n in all_action_names
            )

            started = step_started_at.get("t")
            duration_s = (
                round((datetime.now(timezone.utc) - started).total_seconds(), 1)
                if started
                else None
            )
            row_data: dict[str, Any] = {
                "step": step_count,
                "duration_s": duration_s,
                "category": category,
                "action": action_name,
                "code": is_code,
            }
            if all_action_names:
                row_data["actions"] = executed_actions
                row_data["args"] = executed_args
            if error_action:
                row_data["error_action"] = error_action
            if full_error and len(full_error) > len(summary):
                row_data["error_full"] = full_error[:6000]
            if action_name == "done" and review_state["round"]:
                changed = bool(
                    store is not None
                    and store.read_output() != review_state.get("snapshot")
                )
                row_data["review_reply"] = "resubmitted" if changed else "replied"
            if step.model_output is not None:
                for key, src in (
                    ("see", "what_i_see"),
                    ("plan", "plan_to_goal"),
                    ("next", "next_move"),
                    ("thinking", "thinking"),
                ):
                    val = getattr(step.model_output, src, None)
                    if val:
                        row_data[key] = str(val)[:1500]
            # @nonobvious(forced-by): some models reply with a bare action and no
            # prose at all; the action's own output is the only honest narrative
            # left, so it becomes the step's expandable card.
            if not any(k in row_data for k in ("see", "plan", "next", "thinking")):
                for result in step.result or []:
                    if result.extracted_content:
                        row_data["result_snippet"] = str(result.extracted_content)[:600]
                        break
            native_reasoning = getattr(llm, "_last_model_reasoning", None)
            if native_reasoning:
                llm._last_model_reasoning = None
                reasoning_seconds = getattr(llm, "_last_reasoning_seconds", None)
                llm._last_reasoning_seconds = None
                reasoning_text = str(native_reasoning)
                reasoning_data = {
                    "category": "reasoning",
                    "action": "model_reasoning",
                    "reasoning": reasoning_text[:_REASONING_MAX_CHARS],
                }
                if reasoning_seconds:
                    reasoning_data["duration_s"] = reasoning_seconds
                await crud.create_message(
                    session_id=session_id,
                    role="ai",
                    msg_type="event",
                    data=json.dumps(reasoning_data),
                    summary=_reasoned_title(reasoning_seconds),
                    count_step=False,
                )
            await crud.create_message(
                session_id=session_id,
                role="ai",
                data=json.dumps(row_data),
                msg_type=msg_type,
                summary=summary or action_name or f"Step {step_count}",
            )
            llm._last_action = action_name
            set_activity(session_id, "Running actions", spin=True)

            usage_history = agent_instance.token_cost_service.usage_history
            llm_cost = carried["llm"] + cost.history_cost(usage_history)
            capsolver_cost = carried["capsolver"] + sum(capsolver_costs)
            total_cost = llm_cost + capsolver_cost
            # @nonobvious(must-hold): the budget comes back off the row this
            # write returns, never from a value read when the turn began. A
            # follow-up's budget is written by its caller moments after the
            # parked worker is woken, so anything captured earlier can be a
            # pot behind, and a cap raised to rescue a running task would be
            # ignored until it no longer mattered.
            row = await crud.update_session(
                session_id,
                llm_cost_usd=llm_cost,
                capsolver_cost_usd=capsolver_cost,
                total_cost_usd=total_cost,
                total_input_tokens=int(
                    carried["input_tokens"]
                    + sum((u.usage.prompt_tokens or 0) for u in usage_history if u.usage)
                ),
                total_output_tokens=int(
                    carried["output_tokens"]
                    + sum((u.usage.completion_tokens or 0) for u in usage_history if u.usage)
                ),
            )
            budget = (row or {}).get("max_cost_usd")
            budget_guard.budget = float(budget) if budget else None
            if budget and total_cost >= budget:
                raise BudgetExceededError(
                    f"Cost ${total_cost:.4f} exceeded budget ${budget:.2f}"
                )

        agent_kwargs: dict[str, Any] = {
            "task": prompt,
            "llm": llm,
            "browser": browser_session,
            "tools": tools,
            "calculate_cost": True,
            "llm_timeout": 180,
            # @nonobvious(forced-by): must exceed llm_timeout + the 300s sandbox
            # cap, or step_timeout kills long sandbox scripts mid-run.
            "step_timeout": 520,
            # @nonobvious(means): lets store/file work batch into one LLM step;
            # the chain still truncates at the first page-changing action.
            "max_actions_per_step": 8,
            # @nonobvious(forced-by): default URL shortening corrupts long UUID
            # query params in the LLM's view; a huge limit disables it.
            "_url_shortening_limit": 100_000,
        }
        extension_parts = [
            system_prompt_extension,
            _ACTION_CONTRACT_EXTENSION,
            _CARDS_EXTENSION,
            _DRILL_IN_EXTENSION,
            _TOOLS_EASIEST_EXTENSION,
            _full_toolbox_extension(tools),
            _OVERLAY_EXTENSION,
            _CLIPBOARD_EXTENSION,
            _CODE_REUSE_EXTENSION,
        ]
        # @nonobvious(must-hold): the stock prompt tells the model four times over
        # that captchas are handled for it. That is false here whether or not a solver
        # is configured, and it is worse without one: the model waits for a solve that
        # can never arrive, or quietly reroutes and calls that success.
        solving_available = bool(settings.capsolver_api_key)
        extension_parts.append(
            _CAPTCHA_EXTENSION if solving_available else _CAPTCHA_UNAVAILABLE_EXTENSION
        )
        # @nonobvious(mirrors): the Agent forces flash mode for its own provider's
        # models, and parses replies against the schema its template describes.
        flash_mode = bool(agent_kwargs.get("flash_mode", False)) or (
            getattr(llm, "provider", "") == "browser-use"
        )
        corrected, claim_hits = _captcha_corrected_system_prompt(
            llm,
            agent_kwargs["max_actions_per_step"],
            solving_available=solving_available,
            use_thinking=bool(agent_kwargs.get("use_thinking", True)),
            flash_mode=flash_mode,
        )
        if corrected and claim_hits:
            agent_kwargs["override_system_message"] = corrected
            logger.info("corrected %d CAPTCHA claims in the system prompt", claim_hits)
        # @nonobvious(must-hold): a claim reworded upstream leaves the literals only
        # half matched, so what survived has to be read off the result rather than
        # inferred from the number that matched.
        if corrected is None or _STALE_CAPTCHA_CLAIM_RE.search(corrected):
            await _captcha_progress(
                "Could not fully correct the model's built-in CAPTCHA instructions, "
                "so it may still believe challenges are handled for it."
            )
        if store is not None:
            extension_parts += [_OUTPUT_STORE_EXTENSION, _VERIFY_EXTENSION]
        extension_parts.append(_BEGIN_EXTENSION)
        agent_kwargs["extend_system_message"] = "\n\n".join(p for p in extension_parts if p)
        if sensitive_data:
            agent_kwargs["sensitive_data"] = sensitive_data

        screenshot_action_entry = tools.registry.registry.actions.get("screenshot")
        agent = Agent(**agent_kwargs)
        _restore_screenshot_action(tools, screenshot_action_entry, agent)
        budget_guard.bind(agent.token_cost_service)
        entry = live.register(session_id, agent)
        if store is not None and agent.file_system is not None:
            try:
                await agent.file_system.write_file("output.json", store.read_output())
            except Exception:
                logger.debug("initial output.json mirror failed", exc_info=True)
        steps_before = 0
        while True:
            history = await _run_with_review(
                agent,
                store,
                session_id,
                lambda: agent.run(
                    max_steps=_turn_step_cap(agent),
                    on_step_start=on_step_start,
                    on_step_end=on_step_end,
                ),
                review_state,
            )
            # @nonobvious(means): a stop with strategy "task" lands in the row
            # while the turn is running, so whether this session parks or ends
            # is decided by what the row says now, not by what it said when the
            # worker started.
            refreshed = await crud.get_session(session_id)
            if refreshed:
                keep_alive = bool(refreshed.get("keep_alive"))
            await _finalise_task(
                session_id=session_id,
                agent=agent,
                history=history,
                llm=llm,
                store=store,
                output_model=output_model,
                output_schema=output_schema,
                clipboard=clipboard,
                capsolver_costs=capsolver_costs,
                carried=carried,
                steps_before=steps_before,
                final_status=None if keep_alive else "stopped",
            )
            if not keep_alive or entry.release.is_set():
                break

            # @nonobvious(must-hold): the row only says 'idle' once the worker is
            # parked, because 'idle' is what offers the user a follow-up box and
            # a message arriving before the park would be refused as busy.
            live.park(entry)
            clear_activity(session_id)
            await crud.update_session(session_id, status="idle")
            follow_up = await _wait_for_followup(
                entry, settings.keep_alive_idle_timeout
            )
            if follow_up is None:
                break

            await crud.update_session(session_id, task=follow_up, status="running")
            steps_before = len(agent.history.history)
            carried["capsolver"] += sum(capsolver_costs)
            capsolver_costs.clear()
            prompt = await _prepare_task(
                session_id=session_id,
                task=follow_up,
                url_text=follow_up,
                clipboard=clipboard,
                review_state=review_state,
                preflight=_north_star_preflight(
                    requested_model,
                    f"Earlier in this session they asked: {task}\nNow they ask: {follow_up}",
                ),
                output_schema=output_schema,
                output_model=output_model,
                preamble=_CONTINUATION_PREFIX,
            )
            task = follow_up
            _inject_followup_task(agent, prompt)

        if keep_alive:
            await _record_release(session_id, entry)

    except asyncio.CancelledError:
        # @nonobvious(forced-by): a cancelled worker leaves the row saying 'idle',
        # which offers follow-ups nothing is left to answer; shielded because the
        # write itself would otherwise be cancelled at its first await.
        try:
            await asyncio.shield(crud.update_session(session_id, status="stopped"))
        except Exception:
            logger.debug("cancelled-session status write failed", exc_info=True)
        raise
    except BudgetExceededError as e:
        logger.info("Session %s stopped: %s", session_id, e)
        output, is_successful = _budget_salvage(agent, store, clipboard, output_model)
        items = store.item_count() if store is not None else 0
        if is_successful:
            kept = "The output store was complete and valid, so it stands as the answer."
        elif output and items:
            kept = f"A partial output of {items} item{'' if items == 1 else 's'} is kept."
        elif output:
            kept = "A partial output is kept."
        else:
            kept = "There was no output to keep."
        await crud.update_session(
            session_id,
            status="idle" if keep_alive else "stopped",
            output=output,
            is_task_successful=int(is_successful),
            failure_kind="budget_exceeded",
            failure_status_code=None,
        )
        await crud.create_message(
            session_id=session_id,
            role="ai",
            msg_type="completion",
            summary=f"Stopped: {e}. {kept}",
        )
    except Exception as e:
        logger.exception("Agent session %s failed: %s", session_id, e)
        failure_kind, failure_status_code, failure_status = _failure_info(e)
        await crud.update_session(
            session_id,
            status=failure_status,
            failure_kind=failure_kind,
            failure_status_code=failure_status_code,
        )
        await crud.create_message(
            session_id=session_id,
            role="ai",
            msg_type="browser_action_error",
            summary=f"Fatal error: {str(e)[:200]}",
            data=traceback.format_exc(),
        )
    finally:
        session_ended(session_id)
        if claimed_profile_id:
            release_profile(claimed_profile_id, session_id)
        clear_activity(session_id)
        live.unregister(entry)
        if north_star_task is not None and not north_star_task.done():
            north_star_task.cancel()
        if browser_session:
            # @nonobvious(forced-by): stop() saves full storage state while CDP
            # is live; export_storage_state here would wipe imported
            # localStorage. Shielded + locked against shutdown-cancel truncation.
            try:
                if storage_state_path:
                    async with _storage_lock(storage_state_path):
                        await asyncio.shield(browser_session.stop())
                else:
                    await asyncio.shield(browser_session.stop())
            except Exception:
                logger.warning(
                    "Failed to stop browser session %s", session_id, exc_info=True
                )
        if slot:
            await stop_chrome(slot)
            await display_manager.release(slot.display_num)
