"""First-run setup screen: generates the API key and writes ``.env`` on an
instance that has no authentication configured yet. Once any credential exists
the routes redirect to the dashboard, so a live instance never exposes them.
"""

from __future__ import annotations

import secrets
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.config import settings

_env_path = Path(__file__).resolve().parent.parent.parent / ".env"
_template_dir = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_template_dir))

setup_router = APIRouter(tags=["setup"])


def _configured() -> bool:
    return bool(settings.api_key or settings.dashboard_password)


@setup_router.get("/setup", response_class=HTMLResponse)
async def setup_form(request: Request):
    if _configured():
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request,
        "setup.html",
        context={"generated_key": secrets.token_urlsafe(32), "done": False},
    )


@setup_router.post("/setup", response_class=HTMLResponse)
async def setup_save(
    request: Request,
    api_key: str = Form(...),
    anthropic_api_key: str = Form(""),
    openai_api_key: str = Form(""),
    gemini_api_key: str = Form(""),
    capsolver_api_key: str = Form(""),
    dashboard_password: str = Form(""),
    max_concurrent_sessions: str = Form(""),
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
    lines = [f"API_KEY={api_key}"]
    for name, value in (
        ("ANTHROPIC_API_KEY", anthropic_api_key),
        ("OPENAI_API_KEY", openai_api_key),
        ("GEMINI_API_KEY", gemini_api_key),
        ("CAPSOLVER_API_KEY", capsolver_api_key),
        ("DASHBOARD_PASSWORD", dashboard_password),
    ):
        if value.strip():
            lines.append(f"{name}={value.strip()}")
    if max_concurrent_sessions.strip().isdigit():
        lines.append(f"MAX_CONCURRENT_SESSIONS={max_concurrent_sessions.strip()}")
    _env_path.write_text("\n".join(lines) + "\n")
    return templates.TemplateResponse(
        request,
        "setup.html",
        context={"done": True, "api_key": api_key},
    )
