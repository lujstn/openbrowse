"""First-run setup screen: generates the API key and writes ``.env`` on an
instance that has no authentication configured yet. Once any credential exists
the routes redirect to the dashboard, so a live instance never exposes them.
The capacity section probes the host so the suggested concurrency fits the
machine, from a single-core board to a many-core server.
"""

from __future__ import annotations

import secrets
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from openbrowse import hostinfo
from openbrowse.config import settings
from openbrowse.dashboard.lifecycle import schedule_restart

_env_path = settings.env_path
_template_dir = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_template_dir))

setup_router = APIRouter(tags=["setup"])


def _configured() -> bool:
    return bool(settings.api_key or settings.dashboard_password)


def _capacity_context() -> dict:
    info = hostinfo.probe()
    return {
        "hw_summary": hostinfo.summary(info),
        "hw_complete": info.complete,
        "hw_hard_max": hostinfo.hard_max(info) if info.complete else None,
        "hw_recs": hostinfo.recommendations(info),
        "hw_checklist": hostinfo.checklist(info),
        "hw_systemd": info.systemd,
        "hw_light_recommended": hostinfo.light_flags_recommended(info),
    }


@setup_router.get("/setup", response_class=HTMLResponse)
async def setup_form(request: Request):
    if _configured():
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request,
        "setup.html",
        context={
            "generated_key": secrets.token_urlsafe(32),
            "done": False,
            **_capacity_context(),
        },
    )


@setup_router.post("/setup", response_class=HTMLResponse)
async def setup_save(
    request: Request,
    api_key: str = Form(...),
    anthropic_api_key: str = Form(""),
    openai_api_key: str = Form(""),
    capsolver_api_key: str = Form(""),
    dashboard_password: str = Form(""),
    max_concurrent_sessions: str = Form(""),
    share: str = Form("most"),
    chrome_light_flags: str = Form(""),
):
    if _configured():
        return RedirectResponse("/", status_code=303)
    if _env_path.exists() and _env_path.read_text().strip():
        return HTMLResponse(
            "A non-empty .env already exists; edit it directly instead.",
            status_code=409,
        )
    api_key = api_key.strip()
    if not api_key:
        return HTMLResponse("API key must not be empty.", status_code=400)
    if share not in hostinfo.SHARE_PRESETS:
        share = "most"
    lines = [f"API_KEY={api_key}"]
    for name, value in (
        ("ANTHROPIC_API_KEY", anthropic_api_key),
        ("OPENAI_API_KEY", openai_api_key),
        ("CAPSOLVER_API_KEY", capsolver_api_key),
        ("DASHBOARD_PASSWORD", dashboard_password),
    ):
        if value.strip():
            lines.append(f"{name}={value.strip()}")
    capacity = _capacity_context()
    if max_concurrent_sessions.strip().isdigit():
        value = int(max_concurrent_sessions.strip())
        if capacity["hw_hard_max"]:
            value = max(1, min(value, capacity["hw_hard_max"]))
        lines.append(f"MAX_CONCURRENT_SESSIONS={value}")
    if chrome_light_flags == "1":
        lines.append("CHROME_LIGHT_FLAGS=1")
    tmp = _env_path.with_suffix(_env_path.suffix + ".tmp")
    tmp.write_text("\n".join(lines) + "\n")
    tmp.replace(_env_path)
    return templates.TemplateResponse(
        request,
        "setup.html",
        context={
            "done": True,
            "api_key": api_key,
            "share": share,
            **capacity,
        },
    )


@setup_router.post("/setup/restart", response_class=HTMLResponse)
async def setup_restart(request: Request):
    if _configured():
        return RedirectResponse("/", status_code=303)
    if not (_env_path.exists() and _env_path.read_text().strip()):
        return HTMLResponse("Save the configuration first.", status_code=400)
    schedule_restart()
    return templates.TemplateResponse(
        request, "restarting.html", context={"saved_at": 0, "next_url": "/"}
    )
