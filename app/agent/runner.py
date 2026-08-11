"""Agent execution engine — wraps browser-use Agent with real-time message streaming."""

from __future__ import annotations

import asyncio
import json
import logging
import traceback
from datetime import datetime, timezone
from typing import Any

from browser_use import Agent, BrowserSession, ChatAnthropic, ChatOpenAI, Tools
from browser_use.llm import UserMessage
from browser_use.llm.exceptions import ModelOutputTruncatedError

from app.agent import cost
from app.agent.activity import clear_activity, set_activity
from app.agent.leak_repair import is_missing_action_error, repair_anthropic_message
from app.agent.schema import json_schema_to_pydantic
from app.agent.tools import register_capsolver_tool, register_fetch_tool, register_python_sandbox_tool
from app.browser.factory import display_manager, launch_chrome, stop_chrome
from app.browser.vnc import wait_for_novnc
from app.config import settings
from app.db import crud

logger = logging.getLogger(__name__)

ONE_M_BETA = "context-1m-2025-08-07"


class BudgetExceededError(Exception):
    """Raised when a session exceeds its max_cost_usd budget."""


class _CacheAwareChatOpenAI(ChatOpenAI):
    """ChatOpenAI that also records OpenAI cache-write tokens, which browser-use drops."""

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


class _RepairingChatAnthropic(ChatAnthropic):
    """ChatAnthropic hardened three ways: (1) recover the action list Claude
    sometimes serialises into the AgentOutput ``thinking`` field so the forced
    tool call validates without dropping ``thinking``; (2) retry once with a
    correction if a leak can't be salvaged; (3) recover from output truncation by
    retrying once with streaming + a higher ``max_tokens`` (the non-streaming API
    refuses >~16k, and browser-use's own retry re-runs at the same cap forever).
    """

    async def _create_message(self, **params: Any) -> Any:
        if getattr(self, "_force_stream", False) or (params.get("max_tokens") or 0) > 16384:
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
                return await stream.get_final_message()
        async with client.messages.stream(**params) as stream:
            return await stream.get_final_message()

    async def ainvoke(self, messages: Any, output_format: Any = None, **kwargs: Any) -> Any:
        sid = getattr(self, "_activity_session", None)
        if sid:
            set_activity(sid, "Waiting for model response")
        try:
            try:
                return await super().ainvoke(messages, output_format, **kwargs)
            except Exception as e:
                if output_format is not None and is_missing_action_error(e):
                    logger.info("Retrying LLM call once after action-leak parse failure")
                    correction = UserMessage(
                        content=(
                            "Your previous response placed the action inside the "
                            "'thinking' field. Respond again with the action in the "
                            "structured 'action' field of the tool call, not as text."
                        )
                    )
                    return await super().ainvoke(
                        list(messages) + [correction], output_format, **kwargs
                    )
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
                set_activity(sid, "Acting")


_ANTHROPIC_MODELS: dict[str, str] = {
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
    "bu-max": "claude-sonnet-5",
    "bu-ultra": "claude-opus-4-8",
}

_OPENAI_MODELS: dict[str, str] = {
    "gpt-5.6": "gpt-5.6-sol",
    "gpt-5.6-sol": "gpt-5.6-sol",
    "gpt-5.6-terra": "gpt-5.6-terra",
    "gpt-5.6-luna": "gpt-5.6-luna",
    "bu-mini": "gpt-5.6-luna",
}

_THINKING_BUDGETS: dict[str, int] = {
    "low": 2048,
    "medium": 8192,
    "high": 16384,
}

_ADAPTIVE_THINKING_MODELS = {
    "claude-opus-4-8",
    "claude-sonnet-5",
    "claude-opus-5",
    "claude-fable-5",
    "claude-mythos-5",
}

_OPENAI_REASONING: dict[str, str] = {
    "off": "none",
    "low": "low",
    "medium": "medium",
    "high": "high",
}


def _resolve_model(model: str) -> tuple[str, str]:
    key = (model or "").strip()
    if key.endswith("[1m]"):
        key = key[:-4]
    if key in _ANTHROPIC_MODELS:
        return "anthropic", _ANTHROPIC_MODELS[key]
    if key in _OPENAI_MODELS:
        return "openai", _OPENAI_MODELS[key]
    if key.startswith(("gpt", "o1", "o3", "o4", "chatgpt")):
        return "openai", key
    return "anthropic", key


def _build_llm(model: str, thinking_effort: str) -> tuple[str, str, Any]:
    want_1m = (model or "").strip().endswith("[1m]")
    provider, model_id = _resolve_model(model)
    if provider == "openai":
        if not settings.openai_api_key:
            raise ValueError(f"Model '{model}' needs OPENAI_API_KEY, which is not configured")
        llm = _CacheAwareChatOpenAI(
            model=model_id,
            api_key=settings.openai_api_key,
            reasoning_effort=_OPENAI_REASONING.get(thinking_effort, "low"),
            timeout=90,
            max_retries=3,
        )
        return provider, model_id, llm

    if not settings.anthropic_api_key:
        raise ValueError(f"Model '{model}' needs ANTHROPIC_API_KEY, which is not configured")
    kwargs: dict[str, Any] = {
        "model": model_id,
        "api_key": settings.anthropic_api_key,
        "timeout": 90,
        "max_retries": 3,
        "max_tokens": 16384,
    }
    if want_1m:
        kwargs["betas"] = [ONE_M_BETA]
    if thinking_effort != "off":
        if model_id in _ADAPTIVE_THINKING_MODELS:
            kwargs["thinking"] = {"type": "adaptive"}
            kwargs["output_config"] = {"effort": thinking_effort}
        else:
            budget = _THINKING_BUDGETS.get(thinking_effort, 8192)
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


def _describe_actions(actions: list) -> str:
    parts: list[str] = []
    for action in actions:
        try:
            dumped = action.model_dump(exclude_none=True)
        except Exception:
            parts.append(action.__class__.__name__)
            continue
        if not dumped:
            continue
        name, params = next(iter(dumped.items()))
        detail = ""
        if isinstance(params, dict):
            for pk in ("url", "index", "text", "query", "selector", "seconds"):
                value = params.get(pk)
                if value not in (None, ""):
                    val = str(value)
                    detail = " " + (val[:500] + "…" if len(val) > 500 else val)
                    break
        parts.append(f"{name}{detail}")
    return ", ".join(parts) if parts else "step"


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
    if any(k in n for k in ("navigate", "go_to", "go_back", "search", "switch")):
        return "navigation"
    if any(k in n for k in ("click", "input", "scroll", "send_keys", "select", "dropdown", "upload", "type")):
        return "interaction"
    if any(k in n for k in ("evaluate", "python", "execute_js")):
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
    thinking_effort = session.get("thinking_effort") or "off"
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
        # Allocate virtual display and launch Chrome
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

        # Connect BrowserSession to Chrome via CDP
        browser_session = BrowserSession(
            cdp_url=cdp_url,
            storage_state=storage_state_path,
        )

        # Create tools and register custom actions
        tools = Tools()
        register_fetch_tool(tools)
        register_python_sandbox_tool(tools)
        capsolver_costs: list[float] = []
        register_capsolver_tool(tools, capsolver_costs)

        output_model: type | None = None
        if output_schema:
            try:
                output_model = json_schema_to_pydantic(output_schema, "TaskOutput")
            except Exception as e:
                logger.warning(
                    "output_schema -> model conversion failed, using prose fallback: %s", e
                )
                output_model = None

        full_task = task
        if output_schema and output_model is None:
            schema_str = json.dumps(output_schema, indent=2)
            full_task = (
                f"{task}\n\n"
                f"OUTPUT FORMAT: Return your result as JSON conforming to this schema:\n"
                f"```json\n{schema_str}\n```"
            )

        # Step callback for real-time dashboard streaming
        step_count = 0
        step_started_at: dict[str, Any] = {"t": None}

        async def on_step_start(agent_instance: Agent) -> None:
            step_started_at["t"] = datetime.now(timezone.utc)
            set_activity(session_id, "Preparing next step")

        async def on_step_end(agent_instance: Agent) -> None:
            nonlocal step_count
            step_count += 1
            steps = agent_instance.history.history
            if not steps:
                return
            step = steps[-1]

            summary = ""
            msg_type = "browser_action"

            if step.model_output:
                brain = getattr(step.model_output, "current_state", None)
                if brain and getattr(brain, "next_goal", None):
                    summary = brain.next_goal
                if not summary and step.model_output.action:
                    summary = _describe_actions(step.model_output.action)

            if step.result:
                for result in step.result:
                    if result.error:
                        msg_type = "browser_action_error"
                        summary = f"Error: {_friendly_error(result.error)}"
                    elif result.extracted_content:
                        msg_type = "result"

            action_name = None
            category = None
            if step.model_output and step.model_output.action:
                action_name = _primary_action_name(step.model_output.action)
                category = _category_for(action_name)

            started = step_started_at.get("t")
            duration_s = (
                round((datetime.now(timezone.utc) - started).total_seconds(), 1)
                if started
                else None
            )
            await crud.create_message(
                session_id=session_id,
                role="ai",
                data=json.dumps(
                    {
                        "step": step_count,
                        "duration_s": duration_s,
                        "category": category,
                        "action": action_name,
                    }
                ),
                msg_type=msg_type,
                summary=summary or f"Step {step_count}",
            )
            set_activity(session_id, "Working")

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

        # Build and run agent
        agent_kwargs: dict[str, Any] = {
            "task": full_task,
            "llm": llm,
            "browser": browser_session,
            "tools": tools,
            "calculate_cost": True,
        }
        if output_model is not None:
            agent_kwargs["output_model_schema"] = output_model
        if system_prompt_extension:
            agent_kwargs["extend_system_message"] = system_prompt_extension
        if sensitive_data:
            agent_kwargs["sensitive_data"] = sensitive_data

        agent = Agent(**agent_kwargs)
        history = await agent.run(on_step_start=on_step_start, on_step_end=on_step_end)

        # Extract results
        output = history.final_result() or ""

        schema_valid = True
        if output_model is not None:
            output, schema_valid = await _coerce_to_schema(output, output_model, llm)
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
