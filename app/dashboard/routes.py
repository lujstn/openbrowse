"""Admin dashboard routes — HTMX + SSE for live updates, run form, and live browser feed."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, AsyncGenerator

import httpx
import websockets
from fastapi import APIRouter, Depends, Form, Request, WebSocket
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sse_starlette.sse import EventSourceResponse

from app.agent.pool import pool
from app.auth import dashboard_auth_ok, require_dashboard_auth
from app.config import settings
from app.db import crud

logger = logging.getLogger(__name__)

_template_dir = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_template_dir))

router = APIRouter(tags=["dashboard"], dependencies=[Depends(require_dashboard_auth)])
vnc_router = APIRouter(tags=["dashboard-vnc"])

MODEL_OPTIONS: list[tuple[str, str]] = [
    ("claude-sonnet-5", "Claude Sonnet 5"),
    ("claude-opus-4-8", "Claude Opus 4.8"),
    ("claude-opus-5", "Claude Opus 5"),
    ("gpt-5.6-luna", "GPT-5.6 Luna"),
    ("gpt-5.6-terra", "GPT-5.6 Terra"),
    ("gpt-5.6-sol", "GPT-5.6 Sol"),
]

THINKING_OPTIONS: list[tuple[str, str]] = [
    ("off", "Off"),
    ("low", "Low"),
    ("medium", "Medium"),
    ("high", "High"),
]

_LIVE_STATUSES = ("running",)

_dispatched_tasks: set[asyncio.Task[None]] = set()


def _novnc_port_for_display(display_num: int) -> int:
    return settings.novnc_base_port + (display_num - settings.xvfb_base_display)


async def _novnc_port_for_session(session_id: str) -> int | None:
    session = await crud.get_session(session_id)
    if not session:
        return None
    display_num = session.get("display_num")
    if display_num is None:
        return None
    return _novnc_port_for_display(int(display_num))


def _live_sessions(sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    live = [s for s in sessions if s.get("status") in _LIVE_STATUSES and s.get("live_url")]
    return live[: settings.max_concurrent_sessions]


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


@router.get("/", response_class=HTMLResponse)
async def run_page(request: Request):
    profiles, _ = await crud.list_profiles(page=1, page_size=50)
    return templates.TemplateResponse(
        request,
        "run.html",
        context={
            "profiles": profiles,
            "models": MODEL_OPTIONS,
            "thinking_options": THINKING_OPTIONS,
            "default_model": settings.default_model,
            "active_count": pool.active_count,
            "max_concurrent": settings.max_concurrent_sessions,
        },
    )


@router.post("/run")
async def run_task(
    task: str = Form(...),
    model: str = Form("claude-sonnet-5"),
    profile_id: str = Form(""),
    max_cost_usd: float = Form(0.50),
    keep_alive: bool = Form(False),
    thinking_effort: str = Form("off"),
):
    session = await crud.create_session(
        task=task,
        model=model,
        profile_id=(profile_id or None),
        max_cost_usd=max_cost_usd,
        keep_alive=keep_alive,
        thinking_effort=thinking_effort,
    )
    dispatched = asyncio.create_task(pool.submit(session["id"]))
    _dispatched_tasks.add(dispatched)
    dispatched.add_done_callback(_dispatched_tasks.discard)
    return RedirectResponse(f"/session/{session['id']}", status_code=303)


@router.get("/sessions", response_class=HTMLResponse)
async def sessions_page(request: Request):
    sessions, total = await crud.list_sessions(page=1, page_size=50)
    return templates.TemplateResponse(
        request,
        "sessions.html",
        context={
            "sessions": sessions,
            "live_sessions": _live_sessions(sessions),
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
        request,
        "session_detail.html",
        context={
            "session": session,
            "messages": messages,
            "output_types": ["planning", "result", "completion"],
            "format_duration": _format_duration,
            "format_relative": _format_relative_time,
        },
    )


@router.get("/profiles", response_class=HTMLResponse)
async def profiles_page(request: Request):
    profiles, total = await crud.list_profiles(page=1, page_size=50)
    return templates.TemplateResponse(
        request,
        "profiles.html",
        context={
            "profiles": profiles,
            "total": total,
            "format_relative": _format_relative_time,
        },
    )


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


@router.get("/sse/live-grid")
async def sse_live_grid(request: Request):
    async def event_generator() -> AsyncGenerator[dict[str, str], None]:
        while True:
            if await request.is_disconnected():
                break
            sessions, _ = await crud.list_sessions(page=1, page_size=50)
            payload = [
                {
                    "id": s["id"],
                    "liveUrl": s.get("live_url"),
                    "model": s.get("model"),
                    "status": s.get("status"),
                }
                for s in _live_sessions(sessions)
            ]
            yield {"event": "live-grid", "data": json.dumps(payload)}
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
                        "output": session.get("output") or "",
                    }),
                }
            await asyncio.sleep(1)

    return EventSourceResponse(event_generator())


@vnc_router.get("/vnc/{session_id}/{asset:path}")
async def vnc_asset(request: Request, session_id: str, asset: str):
    if not dashboard_auth_ok(request.headers.get("authorization")):
        return Response(status_code=401, headers={"WWW-Authenticate": "Basic"})
    port = await _novnc_port_for_session(session_id)
    if port is None:
        return Response(status_code=404)
    target = asset or "vnc.html"
    async with httpx.AsyncClient() as client:
        try:
            upstream = await client.get(f"http://127.0.0.1:{port}/{target}", timeout=10.0)
        except httpx.HTTPError:
            return Response(status_code=502)
    headers = {}
    content_type = upstream.headers.get("content-type")
    if content_type:
        headers["content-type"] = content_type
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=headers,
    )


async def _bridge(client_ws: WebSocket, upstream: Any) -> None:
    async def client_to_upstream() -> None:
        try:
            while True:
                msg = await client_ws.receive()
                if msg.get("type") == "websocket.disconnect":
                    break
                if msg.get("bytes") is not None:
                    await upstream.send(msg["bytes"])
                elif msg.get("text") is not None:
                    await upstream.send(msg["text"])
        except Exception:
            pass
        finally:
            try:
                await upstream.close()
            except Exception:
                pass

    async def upstream_to_client() -> None:
        try:
            async for message in upstream:
                if isinstance(message, (bytes, bytearray)):
                    await client_ws.send_bytes(bytes(message))
                else:
                    await client_ws.send_text(message)
        except Exception:
            pass
        finally:
            try:
                await client_ws.close()
            except Exception:
                pass

    await asyncio.gather(client_to_upstream(), upstream_to_client(), return_exceptions=True)


@vnc_router.websocket("/vnc/{session_id}/{ws_path:path}")
async def vnc_ws(websocket: WebSocket, session_id: str, ws_path: str):
    if not dashboard_auth_ok(websocket.headers.get("authorization")):
        await websocket.close(code=1008)
        return
    port = await _novnc_port_for_session(session_id)
    if port is None:
        await websocket.close(code=1011)
        return
    requested = websocket.headers.get("sec-websocket-protocol", "")
    protocols = [p.strip() for p in requested.split(",") if p.strip()]
    subprotocol = "binary" if "binary" in protocols else None
    try:
        async with websockets.connect(
            f"ws://127.0.0.1:{port}/websockify",
            subprotocols=["binary"],
            max_size=None,
            open_timeout=10,
        ) as upstream:
            await websocket.accept(subprotocol=subprotocol)
            await _bridge(websocket, upstream)
    except Exception:
        logger.warning("VNC bridge error for session %s", session_id, exc_info=True)
        try:
            await websocket.close(code=1011)
        except Exception:
            pass
