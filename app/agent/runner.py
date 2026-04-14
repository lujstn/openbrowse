"""Agent execution engine — wraps browser-use Agent with real-time message streaming."""

from __future__ import annotations

import asyncio
import json
import logging
import traceback
from datetime import datetime, timezone
from typing import Any

from browser_use import Agent, BrowserSession, ChatAnthropic, Tools

from app.agent.tools import register_capsolver_tool, register_fetch_tool, register_python_sandbox_tool
from app.browser.factory import display_manager, launch_chrome, stop_chrome
from app.browser.vnc import wait_for_novnc
from app.config import settings
from app.db import crud

logger = logging.getLogger(__name__)


class BudgetExceededError(Exception):
    """Raised when a session exceeds its max_cost_usd budget."""

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

        live_url = f"http://localhost:{slot.novnc_port}/vnc.html?autoconnect=true&resize=scale"
        await crud.update_session(
            session_id,
            status="running",
            display_num=slot.display_num,
            live_url=live_url,
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

        # Create LLM — now from browser-use, not langchain
        llm = ChatAnthropic(
            model=model,
            api_key=settings.anthropic_api_key,
            temperature=0.0,
            timeout=90,
            max_retries=3,
        )

        # Create tools and register custom actions
        tools = Tools()
        register_fetch_tool(tools)
        register_python_sandbox_tool(tools)
        register_capsolver_tool(tools)

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

            # Budget enforcement
            if max_cost:
                usage = agent_instance.history.usage
                if usage and usage.total_cost >= max_cost:
                    raise BudgetExceededError(
                        f"Cost ${usage.total_cost:.4f} exceeded budget ${max_cost:.2f}"
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

        # Cost from built-in tracking
        usage = history.usage
        if usage:
            total_input = usage.total_prompt_tokens
            total_output = usage.total_completion_tokens
            total_cost = usage.total_cost
        else:
            total_input = 0
            total_output = 0
            total_cost = 0.0

        # Validate output against schema if provided
        if output_schema and output:
            try:
                parsed = json.loads(output) if isinstance(output, str) else output
                output = json.dumps(parsed)
            except (json.JSONDecodeError, TypeError):
                pass

        await crud.update_session(
            session_id,
            status="stopped",
            output=output,
            is_task_successful=int(is_successful),
            total_input_tokens=total_input,
            total_output_tokens=total_output,
            llm_cost_usd=total_cost,
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
            try:
                await browser_session.stop()
            except Exception:
                pass
        if slot:
            await stop_chrome(slot)
            await display_manager.release(slot.display_num)
