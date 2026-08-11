"""Agent execution engine — wraps browser-use Agent with real-time message streaming."""

from __future__ import annotations

import asyncio
import json
import logging
import traceback
from datetime import datetime, timezone
from typing import Any

from browser_use import Agent, BrowserSession, ChatAnthropic, ChatOpenAI, Tools

from app.agent import cost
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
            kwargs["max_tokens"] = budget + 8192
    return provider, model_id, ChatAnthropic(**kwargs)


_storage_locks: dict[str, asyncio.Lock] = {}


def _storage_lock(path: str) -> asyncio.Lock:
    lock = _storage_locks.get(path)
    if lock is None:
        lock = asyncio.Lock()
        _storage_locks[path] = lock
    return lock


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

        # Append output schema to task if provided
        full_task = task
        if output_schema:
            schema_str = json.dumps(output_schema, indent=2)
            full_task = (
                f"{task}\n\n"
                f"OUTPUT FORMAT: Return your result as JSON conforming to this schema:\n"
                f"```json\n{schema_str}\n```"
            )

        # Step callback for real-time dashboard streaming
        step_count = 0

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
                    action_names = [a.__class__.__name__ for a in step.model_output.action]
                    summary = ", ".join(action_names)

            if step.result:
                for result in step.result:
                    if result.error:
                        msg_type = "browser_action_error"
                        summary = f"Error: {result.error[:200]}"
                    elif result.extracted_content:
                        msg_type = "result"

            await crud.create_message(
                session_id=session_id,
                role="ai",
                data=json.dumps({"step": step_count}),
                msg_type=msg_type,
                summary=summary or f"Step {step_count}",
            )

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
        if system_prompt_extension:
            agent_kwargs["extend_system_message"] = system_prompt_extension
        if sensitive_data:
            agent_kwargs["sensitive_data"] = sensitive_data

        agent = Agent(**agent_kwargs)
        history = await agent.run(on_step_end=on_step_end)

        # Extract results
        output = history.final_result() or ""
        is_successful = history.is_done() and not history.has_errors()

        usage_history = agent.token_cost_service.usage_history
        llm_cost = cost.history_cost(usage_history, now=datetime.now(timezone.utc))
        capsolver_cost = sum(capsolver_costs)
        total_cost = llm_cost + capsolver_cost
        total_input = sum((u.usage.prompt_tokens or 0) for u in usage_history if u.usage)
        total_output = sum((u.usage.completion_tokens or 0) for u in usage_history if u.usage)

        # Validate output against schema if provided
        if output_schema and output:
            try:
                parsed = json.loads(output) if isinstance(output, str) else output
                output = json.dumps(parsed)
            except (json.JSONDecodeError, TypeError):
                pass

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

        await crud.create_message(
            session_id=session_id,
            role="ai",
            msg_type="completion",
            summary=f"Task {'completed successfully' if is_successful else 'finished with errors'}",
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
