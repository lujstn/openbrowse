"""Admin dashboard routes — HTMX + SSE for live updates, run form, and live browser feed."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Any, AsyncGenerator

import httpx
import websockets
from fastapi import APIRouter, Depends, Form, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sse_starlette.sse import EventSourceResponse

from app.agent.activity import get_activity
from app.agent.pool import pool
from app.agent.runner import _category_for, get_live_agent
from app.api.sessions import _to_session_response
from app.auth import dashboard_auth_ok, require_dashboard_auth
from app.config import settings
from app.db import crud
from app.profiles.storage import cookie_domains, read_state_file

logger = logging.getLogger(__name__)

_template_dir = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_template_dir))


def _safe_fromjson(value: str) -> dict:
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    except (ValueError, TypeError):
        return {}


templates.env.filters["fromjson"] = _safe_fromjson

_SELECTOR_RE = re.compile(
    r"^[a-zA-Z][a-zA-Z0-9]*(\[[a-zA-Z_:][\w:-]*(?:[~^$*|]?=(?:\"[^\"]*\"|'[^']*'|[^\]]*))?\])*$"
)
_SELECTOR_ATTR_RE = re.compile(r"\[([a-zA-Z_:][\w:-]*)(?:[~^$*|]?=(?:\"[^\"]*\"|'[^']*'|[^\]]*))?\]")
_SELECTOR_ACTIONS = {"find_elements", "search_page"}
_CODE_ACTIONS = {"evaluate", "find_elements", "search_page"}


def _htmlify_selector(selector: str) -> str | None:
    sel = selector.strip()
    if not sel or not _SELECTOR_RE.match(sel):
        return None
    tag = re.match(r"^[a-zA-Z][a-zA-Z0-9]*", sel).group(0)
    attrs = _SELECTOR_ATTR_RE.findall(sel)
    return "<" + " ".join([tag] + attrs) + ">"


def message_display(m: dict) -> dict:
    """Category, label, cleaned summary and code-style flag for a feed row, derived
    at render time so it works for old runs (whose stored data predates
    categorisation) as well as new ones."""
    t = m.get("type") or "info"
    summary = m.get("summary") or ""
    if t == "event":
        data = _safe_fromjson(m.get("data") or "")
        return {
            "category": data.get("category", "memory"),
            "label": data.get("action", "note"),
            "summary": summary,
            "code": False,
        }
    if t == "browser_action_error":
        return {"category": "error", "label": "error", "summary": summary, "code": False}
    if t == "planning":
        return {"category": "planning", "label": "planning", "summary": summary, "code": False}
    if t == "completion":
        return {"category": "completion", "label": "done", "summary": summary, "code": False}
    data = _safe_fromjson(m.get("data") or "")
    action = data.get("action")
    category = data.get("category")
    if not action:
        first = summary.split(" ", 1)[0] if summary else ""
        if first and ("_" in first or (first.isalpha() and first.islower())):
            action = first
    if not category:
        category = _category_for(action or summary)

    cleaned = summary
    if action and cleaned.startswith(action + " "):
        cleaned = cleaned[len(action) + 1 :]

    code = bool(data.get("code")) or action in _CODE_ACTIONS

    if action in _SELECTOR_ACTIONS:
        htmlified = _htmlify_selector(cleaned)
        if htmlified:
            cleaned = htmlified
    elif action == "click" and cleaned.strip().isdigit():
        cleaned = "element #" + cleaned.strip()

    cards = {k: data.get(k) for k in ("see", "plan", "next", "thinking") if data.get(k)}
    return {
        "category": category,
        "label": action or category,
        "summary": cleaned,
        "code": code,
        **cards,
    }


templates.env.globals["message_display"] = message_display

router = APIRouter(tags=["dashboard"], dependencies=[Depends(require_dashboard_auth)])
vnc_router = APIRouter(tags=["dashboard-vnc"])

MODEL_OPTIONS: list[tuple[str, str]] = [
    ("claude-sonnet-5", "Claude Sonnet 5"),
    ("gpt-5.6-terra", "GPT-5.6 Terra"),
    ("gpt-5.6-sol", "GPT-5.6 Sol"),
    ("claude-opus-5", "Claude Opus 5"),
    ("claude-opus-4-8", "Claude Opus 4.8"),
    ("claude-opus-4-8[1m]", "Claude Opus 4.8 (1M)"),
    ("claude-opus-4-7", "Claude Opus 4.7"),
    ("claude-opus-4-7[1m]", "Claude Opus 4.7 (1M)"),
    ("claude-opus-4-6", "Claude Opus 4.6"),
    ("claude-opus-4-6[1m]", "Claude Opus 4.6 (1M)"),
    ("claude-sonnet-4-6", "Claude Sonnet 4.6"),
    ("claude-sonnet-4-6[1m]", "Claude Sonnet 4.6 (1M)"),
    ("gpt-5.6-luna", "GPT-5.6 Luna (not recommended)"),
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


def model_provider(model: str | None) -> str:
    key = (model or "").strip()
    if key.endswith("[1m]"):
        key = key[:-4]
    if key.startswith(("gpt", "o1", "o3", "o4", "chatgpt")):
        return "OpenAI"
    return "Anthropic"


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
    output_schema: str = Form(""),
):
    parsed_schema: dict[str, Any] | None = None
    schema_text = (output_schema or "").strip()
    if schema_text:
        try:
            candidate = json.loads(schema_text)
        except json.JSONDecodeError:
            return HTMLResponse("Invalid output schema: not valid JSON", status_code=400)
        if not isinstance(candidate, dict):
            return HTMLResponse(
                "Invalid output schema: must be a JSON object", status_code=400
            )
        parsed_schema = candidate

    session = await crud.create_session(
        task=task,
        model=model,
        profile_id=(profile_id or None),
        output_schema=parsed_schema,
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
            "model_provider": model_provider,
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
            "model_provider": model_provider,
        },
    )


def _strip_thinking(data: str | None) -> str | None:
    """A message's data blob without its raw thinking text, for the steps-only
    export scope. Non-JSON data passes through untouched.
    """
    if not data:
        return data
    try:
        parsed = json.loads(data)
    except (json.JSONDecodeError, TypeError):
        return data
    if isinstance(parsed, dict) and "thinking" in parsed:
        parsed.pop("thinking", None)
        return json.dumps(parsed)
    return data


@router.get("/session/{session_id}/log")
async def session_log(session_id: str, scope: str = "full"):
    """Session export at three scopes: ``output`` is only the schema answer,
    ``steps`` is the session and step log without raw thinking, ``full`` is
    everything the feed shows.
    """
    session = await crud.get_session(session_id)
    if not session:
        return JSONResponse({"error": "Session not found"}, status_code=404)

    if scope == "output":
        raw = session.get("output")
        try:
            return JSONResponse(json.loads(raw) if raw else None)
        except (json.JSONDecodeError, TypeError):
            return JSONResponse({"output": raw})

    messages, _ = await crud.list_messages(session_id, limit=1000)
    export = _to_session_response(session).model_dump()
    export["task"] = session.get("task")
    export["messages"] = [
        {
            "createdAt": m.get("created_at"),
            "type": m.get("type"),
            "summary": m.get("summary"),
            "data": _strip_thinking(m.get("data")) if scope == "steps" else m.get("data"),
        }
        for m in messages
    ]
    return JSONResponse(export)


@router.post("/session/{session_id}/stop")
async def dashboard_stop_session(session_id: str):
    session = await crud.get_session(session_id)
    if not session:
        return JSONResponse({"error": "Session not found"}, status_code=404)
    agent = get_live_agent(session_id)
    if agent is not None:
        try:
            agent.stop()
            return JSONResponse({"ok": True, "action": "stop"})
        except Exception:
            logger.warning("agent.stop() failed for %s; falling back to cancel", session_id, exc_info=True)
    await pool.cancel(session_id)
    await crud.update_session(session_id, status="stopped")
    return JSONResponse({"ok": True, "action": "stop"})


@router.get("/profiles", response_class=HTMLResponse)
async def profiles_page(request: Request):
    profiles, total = await crud.list_profiles(page=1, page_size=50)
    for p in profiles:
        p["cookie_domains"] = cookie_domains(read_state_file(p.get("storage_state_path")))
    return templates.TemplateResponse(
        request,
        "profiles.html",
        context={
            "profiles": profiles,
            "total": total,
            "format_relative": _format_relative_time,
        },
    )


def _profile_state_file(storage_state_path: str):
    return settings.data_dir / storage_state_path


@router.post("/profiles/create")
async def profiles_create(name: str = Form("")):
    profile = await crud.create_profile(name=(name or None))
    state_file = _profile_state_file(profile["storage_state_path"])
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps({"cookies": [], "origins": []}))
    return RedirectResponse("/profiles", status_code=303)


@router.post("/profiles/{profile_id}/edit")
async def profiles_edit(
    profile_id: str,
    name: str = Form(""),
    new_id: str = Form(""),
):
    await crud.update_profile(profile_id, name=(name or None))
    new_id = (new_id or "").strip()
    if new_id and new_id != profile_id:
        existing = await crud.get_profile(profile_id)
        try:
            renamed = await crud.rename_profile(profile_id, new_id)
        except ValueError as exc:
            return HTMLResponse(f"Cannot rename profile: {exc}", status_code=400)
        if renamed and existing and existing.get("storage_state_path"):
            old_file = _profile_state_file(existing["storage_state_path"])
            new_file = _profile_state_file(renamed["storage_state_path"])
            if old_file.exists():
                new_file.parent.mkdir(parents=True, exist_ok=True)
                old_file.replace(new_file)
    return RedirectResponse("/profiles", status_code=303)


@router.post("/profiles/{profile_id}/delete")
async def profiles_delete(profile_id: str):
    profile = await crud.get_profile(profile_id)
    await crud.delete_profile_cascade(profile_id)
    if profile and profile.get("storage_state_path"):
        _profile_state_file(profile["storage_state_path"]).unlink(missing_ok=True)
    return RedirectResponse("/profiles", status_code=303)


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
                model_provider=model_provider,
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
                        "liveUrl": session.get("live_url"),
                        "stepCount": session.get("step_count", 0),
                        "totalInputTokens": session.get("total_input_tokens", 0),
                        "totalOutputTokens": session.get("total_output_tokens", 0),
                        "llmCostUsd": str(session.get("llm_cost_usd", 0)),
                        "capsolverCostUsd": str(session.get("capsolver_cost_usd", 0)),
                        "totalCostUsd": str(session.get("total_cost_usd", 0)),
                        "provider": model_provider(session.get("model")),
                        "output": session.get("output") or "",
                        "activity": get_activity(session_id),
                        "isTaskSuccessful": session.get("is_task_successful"),
                    }),
                }
            await asyncio.sleep(1)

    return EventSourceResponse(event_generator())


_CODEVIEW_HTML = """<!doctype html>
<title>Code</title>
<body style="margin:0;min-height:100vh;background:#0d1117;color:#e6edf3;font-family:ui-monospace,monospace">
<div style="display:flex;align-items:center;gap:12px;padding:14px 20px;background:#161b22;border-bottom:1px solid #30363d">
  <span id="fn" style="color:#8b949e">script.py</span>
  <span id="st" style="margin-left:auto;padding:3px 12px;border-radius:12px;background:#1f6feb;color:#fff;font-size:13px">Writing&hellip;</span>
  <span id="sp" style="display:none;width:16px;height:16px;border:3px solid #30363d;border-top-color:#58a6ff;border-radius:50%;animation:s .8s linear infinite"></span>
</div>
<style>@keyframes s{to{transform:rotate(360deg)}} .caret{display:inline-block;width:8px;background:#58a6ff;animation:b 1s steps(1) infinite} @keyframes b{50%{opacity:0}}</style>
<pre style="margin:0;padding:20px;font-size:14px;line-height:1.5;white-space:pre-wrap;word-break:break-word"><span id="c"></span><span id="caret" class="caret">&nbsp;</span></pre>
<script>
window.__setCode = function(name, code, status){
  document.getElementById('fn').textContent = name;
  document.getElementById('c').textContent = code;
  var st = document.getElementById('st');
  var running = status === 'Running';
  st.textContent = running ? 'Running\\u2026' : 'Writing\\u2026';
  st.style.background = running ? '#238636' : '#1f6feb';
  document.getElementById('sp').style.display = running ? 'inline-block' : 'none';
  document.getElementById('caret').style.display = running ? 'none' : 'inline-block';
  window.scrollTo(0, document.body.scrollHeight);
};
</script>
</body>"""


@vnc_router.get("/codeview")
async def codeview() -> HTMLResponse:
    """The IDE shell shown in the code tab.

    @nonobvious(deliberately-missing): no auth dependency — the shell is static
    and contentless; script text only ever arrives via CDP pushes from the
    platform, so there is nothing here to protect.
    """
    return HTMLResponse(_CODEVIEW_HTML)


_VNC_VIEW_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>live view</title>
<style>html,body{margin:0;height:100%;background:#000;overflow:hidden}div#screen{position:fixed;inset:0;cursor:default;pointer-events:none}div#screen canvas{display:block;cursor:default!important}.overlay{position:fixed;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:14px;background:#000;color:#8a8a8a;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;font-size:13px}.overlay.hidden{display:none}.spinner{width:28px;height:28px;border-radius:50%;border:3px solid rgba(255,255,255,.14);border-top-color:#60a5fa;animation:spin .7s linear infinite}@keyframes spin{to{transform:rotate(360deg)}}.ended-icon{opacity:.85}</style></head>
<body>
  <div id="screen"></div>
  <div id="loading" class="overlay"><div class="spinner"></div><div>Connecting to live view…</div></div>
  <div id="ended" class="overlay hidden">
    <svg width="34" height="34" viewBox="0 0 24 24" fill="none" stroke="#666" stroke-width="1.5" stroke-linecap="round"><rect x="3" y="5" width="18" height="12" rx="2"/><line x1="9" y1="11" x2="15" y2="11"/><line x1="9" y1="20" x2="15" y2="20"/><line x1="12" y1="17" x2="12" y2="20"/></svg>
    <div>Stream ended</div>
  </div>
  <script type="module">
    import RFB from './core/rfb.js';
    function qv(n, d){ const m = location.href.match(new RegExp('[?&]' + n + '=([^&]*)')); return m ? decodeURIComponent(m[1]) : d; }
    const path = qv('path', 'websockify');
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const url = proto + '://' + location.host + '/' + path;
    const loading = document.getElementById('loading');
    const ended = document.getElementById('ended');
    try {
      const rfb = new RFB(document.getElementById('screen'), url);
      rfb.viewOnly = true;
      rfb.showDotCursor = false;
      rfb.scaleViewport = true;
      rfb.addEventListener('connect', () => loading.classList.add('hidden'));
      rfb.addEventListener('disconnect', () => { loading.classList.add('hidden'); ended.classList.remove('hidden'); });
    } catch (e) {
      loading.classList.add('hidden');
      ended.classList.remove('hidden');
    }
  </script>
</body></html>"""


@vnc_router.get("/vnc/{session_id}/view")
async def vnc_view(request: Request, session_id: str):
    if not dashboard_auth_ok(request.headers.get("authorization")):
        return Response(status_code=401, headers={"WWW-Authenticate": "Basic"})
    return HTMLResponse(_VNC_VIEW_HTML)


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
