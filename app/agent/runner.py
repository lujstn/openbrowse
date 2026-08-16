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

from openai import NOT_GIVEN

from browser_use import Agent, BrowserSession, ChatAnthropic, ChatOpenAI, Tools
from browser_use.llm import UserMessage
from browser_use.llm.exceptions import ModelOutputTruncatedError

from app.agent import cost
from app.agent.code_stream import CodeStreamObserver
from app.agent.activity import clear_activity, set_activity
from app.agent.leak_repair import is_missing_action_error, repair_anthropic_message
from app.agent.output_store import OutputStore
from app.agent.schema import json_schema_to_pydantic
from app.agent.tools import (
    TabManager,
    _eval_js,
    register_capsolver_tool,
    register_clipboard_tools,
    register_code_tools,
    register_completeness_gate,
    register_fetch_tool,
    register_output_guard_overrides,
    register_output_store_tools,
    register_tab_tools,
)
from app.browser.factory import display_manager, launch_chrome, stop_chrome
from app.browser.vnc import wait_for_novnc
from app.config import settings
from app.db import crud

logger = logging.getLogger(__name__)

ONE_M_BETA = "context-1m-2025-08-07"

_live_agents: dict[str, Any] = {}


def get_live_agent(session_id: str) -> Any | None:
    """The running browser-use ``Agent`` for a session, or None. Lets the dashboard
    call the agent's native cooperative ``stop()``/``pause()``/``resume()``.
    """
    return _live_agents.get(session_id)

_CARDS_EXTENSION = (
    "Every step, before you act, fill three one-sentence fields: what_i_see (what is "
    "actually on the page now), plan_to_goal (how you get from here to the goal), and "
    "next_move (your next single move). Then emit the action."
)

_DRILL_IN_EXTENSION = (
    "Listing and results pages are a table of contents, not the content. Never "
    "record an item from its snippet; open its own page and read it first."
)

_TOOLS_EASIEST_EXTENSION = (
    "Browsing here is easiest with your own tools, and extraction has ONE golden "
    "path: (1) find_links(...) collects a listing's links with a selector "
    "(href_contains, href_regex, frame_url_contains, container_index, attr) — the "
    "only action that reads links inside an embedded/cross-origin panel; (2) "
    "read_pages() reads every found link in parallel tabs in ONE step, saves "
    "{url, title, text, jsonld, links} per page to pages.json AND prefills "
    "rows_draft.json with one schema row per page; (3) "
    "add_items_from_file('rows_draft.json') loads them all — write NO mapping "
    "script; (4) fix judgement fields in ONE update_items call, deciding from what "
    "you have already read (each page's listing-row text is in page['listing_text'] "
    "in pages.json) — never write a parser script for prose, and NEVER guess an "
    "enum or default one: a value the page does not state stays null; (5) "
    "mark_absent any field NO page publishes — a field found on some pages with "
    "the rest read is already complete as a partial — then done. A record's real "
    "detail lives only on its own page, never the "
    "listing — add_item refuses more than two listing items with no detail. Use "
    "open_tabs/goto_tab/open_in_new_tab/close_tab only when you must interact with "
    "a page; find_elements and evaluate see only the MAIN page, while a script can "
    "read inside an embed with browser.frame_text(url_part)."
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

_NORTH_STAR_PROMPT = (
    "Reply with one sentence stating this task's North Star: what a complete and "
    "correct result looks like, in the task's own words. Name the purpose, not the "
    "output's shape; do not list fields or restate the schema."
)


class BudgetExceededError(Exception):
    """Raised when a session exceeds its max_cost_usd budget."""


_STORE_ONLY_ACTIONS = {
    "add_item",
    "update_item",
    "update_items",
    "set_field",
    "mark_absent",
    "read_output",
    "search_output",
    "add_items_from_file",
    "update_items_from_file",
    "remember",
    "recall",
    "read_file",
    "write_file",
    "replace_file_str",
    "run_code_file",
    "read_pages",
    "http_fetch",
}


def _install_lean_state(browser_session: BrowserSession, flag: dict[str, bool]) -> None:
    """Wrap the session's state fetch so that, when the previous step only did
    store/file/sandbox work (``flag['eligible']``) and the page URL is unchanged,
    the next step gets a stub state — URL, title and tabs, but no DOM listing and no
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

    # @nonobvious(forced-by): BrowserSession is a pydantic model with extra='forbid'
    # and validate_assignment=True, so plain attribute assignment raises; only
    # object.__setattr__ can shadow the method on the instance.
    object.__setattr__(browser_session, "get_browser_state_summary", lean_get)


async def _settle_code_stream(llm: Any, result: Any, output_format: Any) -> None:
    observer = getattr(llm, "stream_observer", None)
    if observer is None or output_format is None:
        return
    from app.agent.code_stream import completion_has_run_code_file

    try:
        await observer.settle(
            completion_has_run_code_file(getattr(result, "completion", None))
        )
    except Exception:
        logger.debug("code stream settle failed", exc_info=True)


class _CacheAwareChatOpenAI(ChatOpenAI):
    """ChatOpenAI that also records OpenAI cache-write tokens, which browser-use drops,
    and streams completions through the code observer when one is attached."""

    def get_client(self) -> Any:
        client = super().get_client()
        observer = getattr(self, "stream_observer", None)
        if observer is not None:
            from app.agent.code_stream import StreamingCompletionsShim

            try:
                client.chat.completions = StreamingCompletionsShim(
                    client.chat.completions, observer
                )
            except Exception:
                logger.debug("streaming shim attach failed", exc_info=True)
        return client

    async def ainvoke(self, messages: Any, output_format: Any = None, **kwargs: Any) -> Any:
        result = await super().ainvoke(messages, output_format, **kwargs)
        await _settle_code_stream(self, result, output_format)
        return result

    def _get_usage(self, response: Any):
        usage = super()._get_usage(response)
        if usage is None or getattr(response, "usage", None) is None:
            return usage
        details = getattr(response.usage, "prompt_tokens_details", None)
        if details is None:
            return usage
        cache_write = getattr(details, "cache_write_tokens", None)
        if cache_write is None:
            extra = getattr(details, "model_extra", None)
            if extra:
                cache_write = extra.get("cache_write_tokens")
        if cache_write:
            usage.prompt_cache_creation_tokens = cache_write
        return usage


_MISSING_ACTION_CORRECTION = (
    'Your reply was rejected: it contained no executable "action" field. The prose '
    "fields (thinking, plan_update, next_goal and so on) describe intent but execute "
    'NOTHING — only the "action" list runs. Respond again with the same content plus '
    '"action": [{"<action_name>": {<parameters>}}]. Do not put action parameters at '
    'the top level: to run code, that is '
    '"action": [{"run_code_file": {"name": "script.py", "code": "..."}}].'
)
_MISSING_ACTION_FINAL = (
    'Rejected again: still no valid "action" field. Reply now with minimal prose and '
    'the "action" list — e.g. {"thinking": "...", "action": [{"<action_name>": '
    "{<parameters>}}]}. Nothing you write executes without it."
)


class _RepairingChatAnthropic(ChatAnthropic):
    """ChatAnthropic hardened three ways: (1) recover the action list Claude
    sometimes serialises into the AgentOutput ``thinking`` field so the forced
    tool call validates without dropping ``thinking``; (2) retry once with a
    correction if a leak can't be salvaged; (3) recover from output truncation by
    retrying once with streaming + a higher ``max_tokens`` (the non-streaming API
    refuses >~16k, and browser-use's own retry re-runs at the same cap forever).
    """

    async def _create_message(self, **params: Any) -> Any:
        if (
            getattr(self, "_force_stream", False)
            or getattr(self, "stream_observer", None) is not None
            or (params.get("max_tokens") or 0) > 16384
        ):
            response = await self._stream_message(**params)
        else:
            response = await super()._create_message(**params)
        try:
            repair_anthropic_message(response)
        except Exception:
            logger.debug("action-leak repair pass failed", exc_info=True)
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
        parts: list[str] = []
        async for event in stream:
            if observer is None:
                continue
            if getattr(event, "type", "") == "input_json" and getattr(
                event, "partial_json", ""
            ):
                parts.append(event.partial_json)
                await observer.on_partial("".join(parts))
        return await stream.get_final_message()

    async def ainvoke(self, messages: Any, output_format: Any = None, **kwargs: Any) -> Any:
        result = await self._ainvoke_inner(messages, output_format, **kwargs)
        await _settle_code_stream(self, result, output_format)
        thinking_text = " ".join((getattr(result, "thinking", None) or "").split())
        if thinking_text:
            self._last_model_thinking = thinking_text
            sid = getattr(self, "_activity_session", None)
            if sid:
                snippet = thinking_text[:140] + ("…" if len(thinking_text) > 140 else "")
                set_activity(sid, f"💭 {snippet}")
        return result

    async def _ainvoke_inner(
        self, messages: Any, output_format: Any = None, **kwargs: Any
    ) -> Any:
        sid = getattr(self, "_activity_session", None)
        if sid:
            last = getattr(self, "_last_action", None)
            label = "Model thinking" + (f" · next step after {last}" if last else "")
            set_activity(sid, label, spin=True)
        try:
            try:
                return await super().ainvoke(messages, output_format, **kwargs)
            except Exception as e:
                if output_format is not None and is_missing_action_error(e):
                    logger.info("Retrying LLM call after missing/malformed action")
                    correction = UserMessage(content=_MISSING_ACTION_CORRECTION)
                    try:
                        return await super().ainvoke(
                            list(messages) + [correction], output_format, **kwargs
                        )
                    except Exception as e2:
                        if not is_missing_action_error(e2):
                            raise
                        logger.info("Second corrective retry after missing action")
                        insist = UserMessage(content=_MISSING_ACTION_FINAL)
                        try:
                            return await super().ainvoke(
                                list(messages) + [correction, insist],
                                output_format,
                                **kwargs,
                            )
                        except Exception as e3:
                            if is_missing_action_error(e3):
                                # @nonobvious(forced-by): the raw pydantic dump would
                                # be replayed into every later step's context; a short
                                # instructive error keeps the failure cheap.
                                raise ValueError(
                                    "Your reply omitted the executable 'action' field "
                                    "three times, so this step was abandoned. Nothing "
                                    "runs without \"action\": [{\"<action_name>\": "
                                    "{...params}}] — include it in your next reply."
                                ) from e3
                            raise
                if isinstance(e, ModelOutputTruncatedError):
                    logger.info("Output truncated; retrying with streaming + max_tokens=64000")
                    prev_mt, prev_to = self.max_tokens, self.timeout
                    self._force_stream = True
                    self.max_tokens = 64000
                    self.timeout = 600
                    try:
                        return await super().ainvoke(messages, output_format, **kwargs)
                    finally:
                        self.max_tokens = prev_mt
                        self.timeout = prev_to
                        self._force_stream = False
                raise
        finally:
            if sid:
                set_activity(sid, "Running actions")


_ANTHROPIC_MODELS: dict[str, str] = {
    "claude-fable-5": "claude-fable-5",
    "claude-mythos-5": "claude-mythos-5",
    "claude-sonnet-5": "claude-sonnet-5",
    "claude-sonnet-4.6": "claude-sonnet-4-6",
    "claude-sonnet-4-6": "claude-sonnet-4-6",
    "claude-opus-5": "claude-opus-5",
    "claude-opus-4.8": "claude-opus-4-8",
    "claude-opus-4-8": "claude-opus-4-8",
    "claude-opus-4.7": "claude-opus-4-7",
    "claude-opus-4-7": "claude-opus-4-7",
    "claude-opus-4.6": "claude-opus-4-6",
    "claude-opus-4-6": "claude-opus-4-6",
    "bu": "claude-sonnet-5",
    "bu-latest": "claude-sonnet-5",
    "bu-ultra": "claude-opus-5",
}

_OPENAI_MODELS: dict[str, str] = {
    "gpt-5.6-sol": "gpt-5.6-sol",
    "gpt-5.6-terra": "gpt-5.6-terra",
    "gpt-5.6-luna": "gpt-5.6-luna",
    "bu-mini": "gpt-5.6-terra",
    "bu-max": "gpt-5.6-sol",
}

_ALWAYS_THINKING_NOTE = (
    "model thinking is always on for this model — the API rejects a disabled "
    "thinking config, so there is no 'off'. The default effort reasons at the "
    "model's own depth; expect thinking tokens in every step."
)

_MODEL_WARNINGS: dict[str, str] = {
    "claude-fable-5": _ALWAYS_THINKING_NOTE,
    "claude-mythos-5": (
        f"{_ALWAYS_THINKING_NOTE} Access is limited to Project Glasswing "
        "organisations; other API keys will be rejected."
    ),
    "gpt-5.6-luna": (
        "expect poor performance — this model often narrates answers instead of "
        "driving the browser and invents nonexistent limits to avoid completing "
        "tasks."
    ),
}

_THINKING_BUDGETS: dict[str, int] = {
    "low": 2048,
    "medium": 8192,
    "high": 16384,
}

_OPENAI_REASONING_HEADROOM: dict[str, int] = {
    "low": 4096,
    "medium": 8192,
    "high": 12288,
    "xhigh": 20480,
}

_FULL_LADDER = ("low", "medium", "high", "xhigh", "max")


@dataclass(frozen=True)
class ModelThinking:
    """One model's provider-side reasoning capability (ModelThinking), as
    distinct from the always-on step reasoning in the feed (BrowserThinking).

    ``default`` is what an unset effort means for this model: "off", a level
    name, or the literal "default" when the provider manages an unnamed depth
    of its own (GPT-5.6 reasons below "low" with the parameter omitted).
    """

    efforts: tuple[str, ...]
    default: str
    can_disable: bool
    style: str  # @nonobvious(means): "adaptive" | "budget" | "openai" wire shape

# @nonobvious(must-hold): rows mirror the live APIs as probed 2026-08-16 —
# omitting `thinking` runs adaptive at "high" on the Claude 5 generation but
# runs WITHOUT thinking on Opus 4.8 and older; Fable/Mythos 400 on a disabled
# config; GPT-5.6 rejects "max" and reasons below "low" when the parameter is
# omitted. Re-probe before editing rows.
_MODEL_THINKING: dict[str, ModelThinking] = {
    "claude-sonnet-5": ModelThinking(_FULL_LADDER, "high", True, "adaptive"),
    "claude-opus-5": ModelThinking(_FULL_LADDER, "high", True, "adaptive"),
    "claude-fable-5": ModelThinking(_FULL_LADDER, "high", False, "adaptive"),
    "claude-mythos-5": ModelThinking(_FULL_LADDER, "high", False, "adaptive"),
    "claude-opus-4-8": ModelThinking(_FULL_LADDER, "off", True, "adaptive"),
    "claude-opus-4-7": ModelThinking(("low", "medium", "high"), "off", True, "budget"),
    "claude-opus-4-6": ModelThinking(("low", "medium", "high"), "off", True, "budget"),
    "claude-sonnet-4-6": ModelThinking(("low", "medium", "high"), "off", True, "budget"),
    "gpt-5.6-terra": ModelThinking(("low", "medium", "high", "xhigh"), "default", True, "openai"),
    "gpt-5.6-sol": ModelThinking(("low", "medium", "high", "xhigh"), "default", True, "openai"),
    "gpt-5.6-luna": ModelThinking(("low", "medium", "high", "xhigh"), "default", True, "openai"),
}


def model_thinking(model: str) -> ModelThinking:
    _, model_id = _resolve_model(model)
    return _MODEL_THINKING[model_id]


def valid_efforts(model: str) -> list[str]:
    spec = model_thinking(model)
    out = ["default"]
    if spec.can_disable:
        out.append("off")
    out.extend(spec.efforts)
    return out


def resolve_default_effort(model: str) -> str:
    return model_thinking(model).default


def validate_effort(model: str, effort: str | None) -> str:
    """The canonical ModelThinkingEffort for this model, or a loud ValueError."""
    key = (effort or "default").strip().lower()
    allowed = valid_efforts(model)
    if key not in allowed:
        raise ValueError(
            f"'{effort}' is not a valid model thinking effort for {model}. "
            f"Valid values: {', '.join(allowed)}."
        )
    return key


def _resolve_model(model: str) -> tuple[str, str]:
    key = (model or "").strip()
    if key.endswith("[1m]"):
        key = key[:-4]
    if key in _ANTHROPIC_MODELS:
        return "anthropic", _ANTHROPIC_MODELS[key]
    if key in _OPENAI_MODELS:
        return "openai", _OPENAI_MODELS[key]
    raise ValueError(f"'{key}' is not a valid model.")


def _build_llm(model: str, thinking_effort: str | None) -> tuple[str, str, Any]:
    want_1m = (model or "").strip().endswith("[1m]")
    provider, model_id = _resolve_model(model)
    effort = validate_effort(model, thinking_effort)
    spec = _MODEL_THINKING[model_id]
    if provider == "openai":
        if not settings.openai_api_key:
            raise ValueError(f"Model '{model}' needs OPENAI_API_KEY, which is not configured")
        if effort == "off":
            reasoning = "none"
        elif effort == "default":
            # @nonobvious(forced-by): browser-use always sends reasoning_effort
            # for reasoning models; NOT_GIVEN is the only way to omit it so the
            # provider's own default applies.
            reasoning = NOT_GIVEN
        else:
            reasoning = effort
        # @nonobvious(forced-by): OpenAI counts reasoning tokens inside
        # max_completion_tokens, so at higher efforts the default 4096 budget
        # is spent on thinking and the structured output truncates mid-JSON.
        completion_budget = 4096 + _OPENAI_REASONING_HEADROOM.get(effort, 0)
        llm = _CacheAwareChatOpenAI(
            model=model_id,
            api_key=settings.openai_api_key,
            reasoning_effort=reasoning,
            max_completion_tokens=completion_budget,
            timeout=90,
            max_retries=3,
            # @nonobvious(forced-by): OpenAI strict structured output requires every
            # object to list all properties as required and forbids free-form dicts,
            # which our action registry cannot satisfy (e.g. add_item's item param);
            # the schema goes into the system prompt and the reply is parsed
            # tolerantly instead — the same trust model as the Anthropic path.
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
    # @nonobvious(forced-by): omission is the only spelling of "model default"
    # the API offers, and on the Claude 5 generation it means adaptive thinking
    # ON at effort high — so "off" must send an explicit disabled config, never
    # omit. validate_effort has already rejected "off" for the models that 400
    # on a disabled config (Fable, Mythos).
    if effort == "off":
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
        base = f"{len(urls)} urls" if urls else "found_links"
        return ((f"{base} (frame: {frame})" if frame else base)[:200], False)
    if name == "update_items" and isinstance(params, dict):
        return (f"{len(params.get('updates') or [])} updates", False)
    if name == "mark_absent" and isinstance(params, dict):
        return (str(params.get("field", ""))[:200], False)
    if name == "run_code_file" and isinstance(params, dict):
        url = params.get("url")
        base = str(params.get("name", ""))
        return ((f"{base} @ {url}" if url else base)[:200], False)

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


def _friendly_error(error: str) -> str:
    return " ".join((error or "").split())[:200]


def _primary_action_name(actions: list) -> str | None:
    if not actions:
        return None
    try:
        dumped = actions[0].model_dump(exclude_none=True)
    except Exception:
        return None
    return next(iter(dumped), None) if dumped else None


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
            [UserMessage(content=f"{_NORTH_STAR_PROMPT}\n\nTASK:\n{task}")]
        )
        text = " ".join((getattr(resp, "completion", None) or str(resp)).split())
        if text:
            return text[:400]
    except Exception:
        logger.debug("North Star pre-flight failed", exc_info=True)
    first = re.split(r"(?<=[.!?])\s", (task or "").strip(), maxsplit=1)[0]
    return first.strip()[:400] or (task or "").strip()[:400]


# @nonobvious(forced-by): "action" sits directly after "thinking" because models
# emit JSON in schema property order and drop trailing properties under output
# pressure — with action last, runs lost whole steps to "action Field required"
# validation errors; the prose cards are optional, so they are the safe tail.
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
                        # @nonobvious(forced-by): cards stay optional — forcing them required under forced tool_choice made Claude bleed its XML tool-call idiom into the JSON string values.
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


async def run_agent_session(session_id: str) -> None:
    """Execute a browser-use agent for the given session. Runs as a background task."""
    session = await crud.get_session(session_id)
    if not session:
        logger.error("Session %s not found", session_id)
        return

    task = session.get("task")
    if not task:
        await crud.update_session(session_id, status="error")
        return

    requested_model = session.get("model") or settings.default_model
    thinking_effort = session.get("thinking_effort") or "default"
    output_schema = json.loads(session["output_schema"]) if session.get("output_schema") else None
    sensitive_data = json.loads(session["sensitive_data"]) if session.get("sensitive_data") else None
    system_prompt_extension = session.get("system_prompt_extension")
    max_cost = session.get("max_cost_usd")

    try:
        provider, model, llm = _build_llm(requested_model, thinking_effort)
    except ValueError as e:
        logger.error("Session %s LLM setup failed: %s", session_id, e)
        await crud.update_session(session_id, status="error")
        await crud.create_message(
            session_id=session_id,
            role="ai",
            msg_type="browser_action_error",
            summary=str(e)[:200],
        )
        return

    llm._activity_session = session_id

    north_star_task: asyncio.Task | None = None
    try:
        preflight_effort = "off" if model_thinking(requested_model).can_disable else "default"
        _, _, preflight_llm = _build_llm(requested_model, preflight_effort)
        try:
            preflight_llm.max_tokens = 300
        except Exception:
            pass
        north_star_task = asyncio.create_task(_derive_north_star(preflight_llm, task))
    except Exception:
        logger.debug("North Star pre-flight setup failed", exc_info=True)
        north_star_task = None

    # Load profile storage state path
    storage_state_path: str | None = None
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

    slot = None
    browser_session = None
    try:
        slot = await display_manager.allocate()
        await wait_for_novnc(slot.novnc_port)
        cdp_url = await launch_chrome(slot)

        live_url = f"/vnc/{session_id}/view?path=vnc/{session_id}/websockify"
        await crud.update_session(
            session_id,
            status="running",
            display_num=slot.display_num,
            live_url=live_url,
            title=(task[:80] if task else None),
        )
        await crud.create_message(
            session_id=session_id,
            role="ai",
            msg_type="planning",
            summary=f"Session started with model {model}",
        )
        warned_model = _resolve_model(model)[1]
        if warned_model in _MODEL_WARNINGS:
            await crud.create_message(
                session_id=session_id,
                role="ai",
                msg_type="event",
                summary=f"⚠️ {warned_model}: {_MODEL_WARNINGS[warned_model]}",
                data=json.dumps({"category": "memory", "action": "modelWarning"}),
                count_step=False,
            )

        browser_session = BrowserSession(
            cdp_url=cdp_url,
            storage_state=storage_state_path,
            cross_origin_iframes=True,
        )

        clipboard: dict[str, Any] = {}
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
            await crud.create_message(
                session_id=session_id,
                role="ai",
                msg_type="event",
                data=json.dumps({"category": "read", "action": "read_pages"}),
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

        code_observer = CodeStreamObserver(browser_session, clipboard, _code_progress)
        object.__setattr__(llm, "stream_observer", code_observer)

        register_fetch_tool(tools)
        register_code_tools(tools, clipboard, store, _code_progress)
        register_clipboard_tools(tools, clipboard)
        register_tab_tools(tools, tab_manager, clipboard, store, _read_progress)
        capsolver_costs: list[float] = []
        register_capsolver_tool(tools, capsolver_costs)

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

            register_completeness_gate(tools, store, _on_incomplete_done, clipboard)

        register_output_guard_overrides(tools)

        north_star = ""
        if north_star_task is not None:
            try:
                north_star = await north_star_task
            except Exception:
                logger.debug("North Star pre-flight await failed", exc_info=True)
        if not north_star:
            north_star = re.split(r"(?<=[.!?])\s", (task or "").strip(), maxsplit=1)[0][:400]
        clipboard["northStar"] = north_star

        full_task = task
        if north_star:
            full_task = f"{task}\n\nNORTH STAR: {north_star}"
            await crud.create_message(
                session_id=session_id,
                role="ai",
                msg_type="event",
                data=json.dumps({"category": "memory", "action": "northStar"}),
                summary=f"North Star: {north_star}",
                count_step=False,
            )
        if output_schema and output_model is None:
            schema_str = json.dumps(output_schema, indent=2)
            full_task = (
                f"{full_task}\n\n"
                f"OUTPUT FORMAT: Return your result as JSON conforming to this schema:\n"
                f"```json\n{schema_str}\n```"
            )

        start_match = re.search(r"https?://[^\s\"'<>)\]]+", task or "")
        if start_match:
            start_url = start_match.group(0).rstrip(".,;)")
            clipboard["startUrl"] = start_url
            await crud.create_message(
                session_id=session_id,
                role="ai",
                msg_type="event",
                data=json.dumps({"category": "memory", "action": "startUrl"}),
                summary=f"startUrl saved → {start_url}",
                count_step=False,
            )

        lean_flag: dict[str, bool] = {"eligible": False}
        _install_lean_state(browser_session, lean_flag)

        step_count = 0
        step_started_at: dict[str, Any] = {"t": None}
        logged_history_len = {"n": 0}

        async def on_step_start(agent_instance: Agent) -> None:
            step_started_at["t"] = datetime.now(timezone.utc)
            set_activity(session_id, "Preparing next step")

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
                    summary=f"startUrl saved → {current_url}",
                    count_step=False,
                )

            steps = agent_instance.history.history
            if not steps:
                return
            # @nonobvious(forced-by): browser-use fires on_step_end even when the
            # step was cancelled by step_timeout, in which case no new history entry
            # exists — re-reading history[-1] would double-log the previous step.
            if len(steps) == logged_history_len["n"]:
                await crud.create_message(
                    session_id=session_id,
                    role="ai",
                    msg_type="browser_action_error",
                    summary="Step timed out and was cancelled before completing",
                )
                return
            logged_history_len["n"] = len(steps)
            step = steps[-1]

            if north_star and step_count % 10 == 0:
                try:
                    agent_instance._message_manager.add_new_task(
                        f"North Star: {north_star} Not done until this is met."
                    )
                except Exception:
                    logger.debug("north star reminder injection failed", exc_info=True)
                await crud.create_message(
                    session_id=session_id,
                    role="ai",
                    msg_type="event",
                    data=json.dumps({"category": "memory", "action": "northStar"}),
                    summary=f"North Star reminder (step {step_count})",
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

            if step.result:
                for result in step.result:
                    if result.error:
                        msg_type = "browser_action_error"
                        summary = f"Error: {_friendly_error(result.error)}"
                        is_code = False
                    elif result.extracted_content:
                        msg_type = "result"

            action_name = None
            category = None
            all_action_names: list[str] = []
            if step.model_output and step.model_output.action:
                action_name = _primary_action_name(step.model_output.action)
                category = _category_for(action_name)
                for act in step.model_output.action:
                    try:
                        dumped = act.model_dump(exclude_none=True)
                    except Exception:
                        continue
                    all_action_names.extend(dumped.keys())
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
            native_thinking = getattr(llm, "_last_model_thinking", None)
            if native_thinking:
                row_data["model_thinking"] = str(native_thinking)[:1500]
                llm._last_model_thinking = None
            await crud.create_message(
                session_id=session_id,
                role="ai",
                data=json.dumps(row_data),
                msg_type=msg_type,
                summary=summary or action_name or f"Step {step_count}",
            )
            llm._last_action = action_name
            set_activity(session_id, "Running actions")

            usage_history = agent_instance.token_cost_service.usage_history
            llm_cost = cost.history_cost(usage_history, now=datetime.now(timezone.utc))
            capsolver_cost = sum(capsolver_costs)
            total_cost = llm_cost + capsolver_cost
            await crud.update_session(
                session_id,
                llm_cost_usd=llm_cost,
                capsolver_cost_usd=capsolver_cost,
                total_cost_usd=total_cost,
                total_input_tokens=sum((u.usage.prompt_tokens or 0) for u in usage_history if u.usage),
                total_output_tokens=sum((u.usage.completion_tokens or 0) for u in usage_history if u.usage),
            )
            if max_cost and total_cost >= max_cost:
                raise BudgetExceededError(
                    f"Cost ${total_cost:.4f} exceeded budget ${max_cost:.2f}"
                )

        agent_kwargs: dict[str, Any] = {
            "task": full_task,
            "llm": llm,
            "browser": browser_session,
            "tools": tools,
            "calculate_cost": True,
            "llm_timeout": 180,
            # @nonobvious(forced-by): browser-use cancels the WHOLE step (LLM call +
            # actions) at step_timeout; the default 180 silently killed 180s sandbox
            # scripts mid-run. Must exceed llm_timeout + the 300s sandbox cap.
            "step_timeout": 520,
            # @nonobvious(means): >1 lets non-page-changing work (update_item runs,
            # store reads, file saves) batch into one LLM step; browser-use itself
            # truncates the chain at the first page-changing action.
            "max_actions_per_step": 8,
            # @nonobvious(forced-by): browser-use middle-shortens long URLs at the
            # LLM-input layer (default 25 chars of query+fragment), which makes the
            # agent read links with long UUID query params as corrupt data; a limit
            # past the longest real URL early-returns the original, no reverse map.
            "_url_shortening_limit": 100_000,
        }
        extension_parts = [
            system_prompt_extension,
            _ACTION_CONTRACT_EXTENSION,
            _CARDS_EXTENSION,
            _DRILL_IN_EXTENSION,
            _TOOLS_EASIEST_EXTENSION,
            _OVERLAY_EXTENSION,
            _CLIPBOARD_EXTENSION,
            _CODE_REUSE_EXTENSION,
        ]
        if store is not None:
            extension_parts += [_OUTPUT_STORE_EXTENSION, _VERIFY_EXTENSION]
        extension_parts.append(_BEGIN_EXTENSION)
        agent_kwargs["extend_system_message"] = "\n\n".join(p for p in extension_parts if p)
        if sensitive_data:
            agent_kwargs["sensitive_data"] = sensitive_data

        agent = Agent(**agent_kwargs)
        _live_agents[session_id] = agent
        if store is not None and agent.file_system is not None:
            try:
                await agent.file_system.write_file("output.json", store.read_output())
            except Exception:
                logger.debug("initial output.json mirror failed", exc_info=True)
        history = await agent.run(on_step_start=on_step_start, on_step_end=on_step_end)

        file_output = ""
        try:
            result_file = agent.file_system.get_file("result.json") if agent.file_system else None
            if result_file:
                file_content = result_file.read()
                if file_content and file_content.strip():
                    file_output = file_content
        except Exception:
            logger.debug("result.json read from agent.file_system failed", exc_info=True)
        done_output = history.final_result() or ""
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

        recovered_errors = sum(1 for e in history.errors() if e)
        is_successful = (
            history.is_done()
            and (history.is_successful() is not False)
            and schema_valid
        )

        usage_history = agent.token_cost_service.usage_history
        llm_cost = cost.history_cost(usage_history, now=datetime.now(timezone.utc))
        capsolver_cost = sum(capsolver_costs)
        total_cost = llm_cost + capsolver_cost
        total_input = sum((u.usage.prompt_tokens or 0) for u in usage_history if u.usage)
        total_output = sum((u.usage.completion_tokens or 0) for u in usage_history if u.usage)

        final_status = "idle" if session.get("keep_alive") else "stopped"
        await crud.update_session(
            session_id,
            status=final_status,
            output=output,
            is_task_successful=int(is_successful),
            total_input_tokens=total_input,
            total_output_tokens=total_output,
            llm_cost_usd=llm_cost,
            capsolver_cost_usd=capsolver_cost,
            total_cost_usd=total_cost,
        )

        judgement = None
        try:
            judgement = getattr(history.history[-1].result[-1], "judgement", None)
        except (IndexError, AttributeError):
            pass
        if judgement is not None and bool(judgement.verdict) != bool(is_successful):
            judge_word = "PASS" if judgement.verdict else "FAIL"
            own_word = "success" if is_successful else "failure"
            reason = " ".join(
                (judgement.failure_reason or judgement.reasoning or "").split()
            )[:400]
            await crud.create_message(
                session_id=session_id,
                role="ai",
                msg_type="event",
                summary=(
                    f"Judge dissent: verdict {judge_word} vs recorded {own_word}"
                    + (f" — {reason}" if reason else "")
                ),
                data=json.dumps({"category": "judge", "action": "verdict"}),
                count_step=False,
            )

        if is_successful:
            completion_summary = "Task completed successfully"
            if recovered_errors:
                plural = "s" if recovered_errors != 1 else ""
                completion_summary += (
                    f" (recovered from {recovered_errors} transient error{plural})"
                )
        else:
            completion_summary = "Task finished with errors"
        await crud.create_message(
            session_id=session_id,
            role="ai",
            msg_type="completion",
            summary=completion_summary,
        )

    except BudgetExceededError as e:
        logger.info("Session %s stopped: %s", session_id, e)
        await crud.update_session(session_id, status="stopped")
        await crud.create_message(
            session_id=session_id,
            role="ai",
            msg_type="completion",
            summary=f"Stopped: {e}",
        )
    except Exception as e:
        logger.exception("Agent session %s failed: %s", session_id, e)
        await crud.update_session(session_id, status="error")
        await crud.create_message(
            session_id=session_id,
            role="ai",
            msg_type="browser_action_error",
            summary=f"Fatal error: {str(e)[:200]}",
            data=traceback.format_exc(),
        )
    finally:
        clear_activity(session_id)
        _live_agents.pop(session_id, None)
        if north_star_task is not None and not north_star_task.done():
            north_star_task.cancel()
        if browser_session:
            # @nonobvious(forced-by) stop() dispatches SaveStorageStateEvent (full cookies+localStorage, merged with the file on disk) while CDP is still live; export_storage_state here instead rewrites the file with origins:[] and wipes imported localStorage. Shielded + per-profile locked so a shutdown cancel can't truncate the save.
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
