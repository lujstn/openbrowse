"""Agent execution engine — wraps browser-use Agent with message capture and token tracking."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import traceback
from datetime import datetime, timezone
from typing import Any

from browser_use import Agent, Browser, Controller
from browser_use.browser.browser import BrowserConfig
from browser_use.browser.context import BrowserContextConfig
from langchain_anthropic import ChatAnthropic

from app.agent.tools import register_capsolver_tool, register_fetch_tool, register_python_sandbox_tool
from app.browser.factory import create_browser_kwargs, display_manager
from app.browser.vnc import wait_for_novnc
from app.config import settings
from app.db import crud

logger = logging.getLogger(__name__)

# Anthropic pricing per million tokens (as of 2026-04)
MODEL_PRICING: dict[str, tuple[float, float]] = {
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-opus-4-6": (15.0, 75.0),
}

MODEL_MAP: dict[str, str] = {
    "claude-sonnet-4.6": "claude-sonnet-4-6",
    "claude-sonnet-4-6": "claude-sonnet-4-6",
    "bu-max": "claude-sonnet-4-6",
    "claude-opus-4.6": "claude-opus-4-6",
    "claude-opus-4-6": "claude-opus-4-6",
    "bu-ultra": "claude-opus-4-6",
}


def _resolve_model(model: str) -> str:
    return MODEL_MAP.get(model, model)


def _calculate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    resolved = _resolve_model(model)
    pricing = MODEL_PRICING.get(resolved, (3.0, 15.0))
    return (input_tokens * pricing[0] + output_tokens * pricing[1]) / 1_000_000


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

    model = _resolve_model(session.get("model", "claude-sonnet-4-6"))
    output_schema = json.loads(session["output_schema"]) if session.get("output_schema") else None
    sensitive_data = json.loads(session["sensitive_data"]) if session.get("sensitive_data") else None
    system_prompt_extension = session.get("system_prompt_extension")
    max_cost = session.get("max_cost_usd")

    # Load profile storage state if specified
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
    browser = None
    try:
        slot = await display_manager.allocate()
        display_num = slot.display_num

        await wait_for_novnc(slot.novnc_port)

        live_url = f"http://localhost:{slot.novnc_port}/vnc.html?autoconnect=true&resize=scale"

        await crud.update_session(
            session_id,
            status="running",
            display_num=display_num,
            live_url=live_url,
        )

        await crud.create_message(
            session_id=session_id,
            role="ai",
            msg_type="planning",
            summary=f"Session started with model {model}",
        )

        # Build BrowserConfig from factory kwargs
        factory_kwargs = create_browser_kwargs(slot)

        # Set DISPLAY env var for the Xvfb virtual display
        env_vars = factory_kwargs.get("env", {})
        for k, v in env_vars.items():
            os.environ[k] = v

        context_config = BrowserContextConfig(
            window_width=factory_kwargs.get("window_size", {}).get("width", 1920),
            window_height=factory_kwargs.get("window_size", {}).get("height", 1080),
        )
        if storage_state_path:
            context_config.cookies_file = storage_state_path

        browser_config = BrowserConfig(
            browser_binary_path=factory_kwargs.get("executable_path"),
            headless=factory_kwargs.get("headless", False),
            extra_browser_args=factory_kwargs.get("args", []),
            new_context_config=context_config,
        )
        browser = Browser(config=browser_config)

        # Create LLM
        llm = ChatAnthropic(
            model=model,
            api_key=settings.anthropic_api_key,
            temperature=0.0,
            timeout=90,
            max_retries=3,
        )

        # Create controller with all three tools
        controller = Controller()
        register_fetch_tool(controller)
        register_python_sandbox_tool(controller)
        register_capsolver_tool(controller)

        # If output_schema provided, include it in the task prompt
        full_task = task
        if output_schema:
            schema_str = json.dumps(output_schema, indent=2)
            full_task = (
                f"{task}\n\n"
                f"OUTPUT FORMAT: Return your result as JSON conforming to this schema:\n"
                f"```json\n{schema_str}\n```"
            )

        # Build agent kwargs
        agent_kwargs: dict[str, Any] = {
            "task": full_task,
            "llm": llm,
            "browser": browser,
            "controller": controller,
        }
        if system_prompt_extension:
            agent_kwargs["extend_system_message"] = system_prompt_extension
        if sensitive_data:
            agent_kwargs["sensitive_data"] = sensitive_data

        agent = Agent(**agent_kwargs)

        # Run the agent
        history = await agent.run()

        # Extract token usage from step metadata
        total_input_tokens = history.total_input_tokens()

        # browser-use doesn't track output tokens separately;
        # estimate as ~30% of input tokens for cost calculation
        estimated_output_tokens = total_input_tokens // 3

        # Extract messages from history
        for i, step in enumerate(history.history):
            msg_type = "browser_action"
            summary = ""

            if step.model_output:
                if hasattr(step.model_output, "current_state"):
                    state = step.model_output.current_state
                    if hasattr(state, "next_goal") and state.next_goal:
                        summary = state.next_goal
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
                data=json.dumps({"step": i + 1}),
                msg_type=msg_type,
                summary=summary or f"Step {i + 1}",
            )

        cost = _calculate_cost(model, total_input_tokens, estimated_output_tokens)

        output = history.final_result() or ""
        is_successful = history.is_done() and not history.has_errors()

        if output_schema and output:
            try:
                parsed = json.loads(output) if isinstance(output, str) else output
                output = json.dumps(parsed)
            except (json.JSONDecodeError, TypeError):
                pass

        # Save cookies back to profile if applicable
        if storage_state_path and browser:
            try:
                browser_context = agent.browser_context
                if browser_context:
                    await browser_context.save_cookies()
            except Exception as e:
                logger.warning("Failed to save cookies: %s", e)

        await crud.update_session(
            session_id,
            status="stopped",
            output=output,
            is_task_successful=int(is_successful),
            total_input_tokens=total_input_tokens,
            total_output_tokens=estimated_output_tokens,
            llm_cost_usd=cost,
            total_cost_usd=cost,
        )

        await crud.create_message(
            session_id=session_id,
            role="ai",
            msg_type="completion",
            summary=f"Task {'completed successfully' if is_successful else 'finished with errors'}",
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
        if browser:
            try:
                await browser.close()
            except Exception:
                pass
        if slot:
            await display_manager.release(slot.display_num)
