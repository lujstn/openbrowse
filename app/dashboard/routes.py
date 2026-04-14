"""Admin dashboard routes — HTMX + SSE for live updates."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, AsyncGenerator

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sse_starlette.sse import EventSourceResponse

from app.db import crud

_template_dir = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_template_dir))

router = APIRouter(tags=["dashboard"])


def _format_duration_secs(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes = int(seconds // 60)
    secs = int(seconds % 60)
    if minutes < 60:
        return f"{minutes}m {secs}s"
    hours = int(minutes // 60)
    mins = minutes % 60
    return f"{hours}h {mins}m"


def _format_duration(created_at: str, updated_at: str, status: str) -> str:
    from datetime import datetime, timezone
    try:
        start = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        if status in ("running", "created", "idle"):
            end = datetime.now(timezone.utc)
        else:
            end = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
        seconds = (end - start).total_seconds()
        return _format_duration_secs(max(0, seconds))
    except (ValueError, TypeError):
        return "—"


def _format_relative_time(iso_str: str) -> str:
    from datetime import datetime, timezone
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        diff = now - dt
        seconds = diff.total_seconds()
        if seconds < 60:
            return "just now"
        if seconds < 3600:
            return f"{int(seconds // 60)} min ago"
        if seconds < 86400:
            return f"{int(seconds // 3600)}h ago"
        return f"{int(seconds // 86400)}d ago"
    except (ValueError, TypeError):
        return iso_str


# ── Pages ────────────────────────────────────────────────────────────


@router.get("/", response_class=HTMLResponse)
async def dashboard_home(request: Request):
    sessions, total = await crud.list_sessions(page=1, page_size=50)
    return templates.TemplateResponse(
        "sessions.html",
        {
            "request": request,
            "sessions": sessions,
            "total": total,
            "format_duration": _format_duration,
            "format_relative": _format_relative_time,
        },
    )


@router.get("/session/{session_id}", response_class=HTMLResponse)
async def session_detail(request: Request, session_id: str):
    session = await crud.get_session(session_id)
    if not session:
        return HTMLResponse("Session not found", status_code=404)
    messages, _ = await crud.list_messages(session_id, limit=500)
    return templates.TemplateResponse(
        "session_detail.html",
        {
            "request": request,
            "session": session,
            "messages": messages,
            "format_duration": _format_duration,
            "format_relative": _format_relative_time,
        },
    )


@router.get("/profiles", response_class=HTMLResponse)
async def profiles_page(request: Request):
    profiles, total = await crud.list_profiles(page=1, page_size=50)
    return templates.TemplateResponse(
        "profiles.html",
        {
            "request": request,
            "profiles": profiles,
            "total": total,
            "format_relative": _format_relative_time,
        },
    )


# ── SSE for live updates ─────────────────────────────────────────────


@router.get("/sse/sessions")
async def sse_sessions(request: Request):
    async def event_generator() -> AsyncGenerator[dict[str, str], None]:
        while True:
            if await request.is_disconnected():
                break
            sessions, total = await crud.list_sessions(page=1, page_size=50)
            rows_html = templates.get_template("_session_rows.html").render(
                sessions=sessions,
                format_duration=_format_duration,
                format_relative=_format_relative_time,
            )
            yield {"event": "sessions", "data": rows_html}
            await asyncio.sleep(2)

    return EventSourceResponse(event_generator())


@router.get("/sse/session/{session_id}/messages")
async def sse_session_messages(request: Request, session_id: str):
    last_count = 0

    async def event_generator() -> AsyncGenerator[dict[str, str], None]:
        nonlocal last_count
        while True:
            if await request.is_disconnected():
                break
            messages, _ = await crud.list_messages(session_id, limit=500)
            if len(messages) > last_count:
                new_msgs = messages[last_count:]
                last_count = len(messages)
                html = templates.get_template("_message_rows.html").render(
                    messages=new_msgs,
                    format_relative=_format_relative_time,
                )
                yield {"event": "messages", "data": html}
            session = await crud.get_session(session_id)
            if session:
                yield {
                    "event": "status",
                    "data": json.dumps({
                        "status": session["status"],
                        "stepCount": session.get("step_count", 0),
                        "totalInputTokens": session.get("total_input_tokens", 0),
                        "totalOutputTokens": session.get("total_output_tokens", 0),
                        "llmCostUsd": str(session.get("llm_cost_usd", 0)),
                    }),
                }
            await asyncio.sleep(1)

    return EventSourceResponse(event_generator())
