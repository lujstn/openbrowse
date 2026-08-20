"""First-run setup wizard: generates the API key and writes ``.env`` on an
instance that has no authentication configured yet. Once any credential exists
the routes redirect to the dashboard, so a live instance never exposes them.
The capacity section probes the host so the suggested concurrency fits the
machine, from a single-core board to a many-core server, and the provider step
checks each key against its provider live before the wizard lets it through.
"""

from __future__ import annotations

import secrets
from pathlib import Path
from typing import Literal

import httpx
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from openbrowse import hostinfo
from openbrowse.config import settings
from openbrowse.dashboard.lifecycle import schedule_restart

_env_path = settings.env_path
_template_dir = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_template_dir))

setup_router = APIRouter(tags=["setup"])

MIN_PASSWORD_LENGTH = 8

_VALIDATE_TIMEOUT_S = 8.0


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
            "min_password_length": MIN_PASSWORD_LENGTH,
            **_capacity_context(),
        },
    )


class _KeyCheck(BaseModel):
    provider: Literal["anthropic", "openai", "capsolver"]
    key: str = Field(min_length=1, max_length=2048)


async def _check_provider_key(provider: str, key: str) -> tuple[bool | None, str]:
    """Ask the provider itself whether the key is real.

    Returns ``(True, "")`` for a live key, ``(False, reason)`` for a rejected
    one, and ``(None, reason)`` when the provider could not be reached, which
    is a fact about the network rather than the key.
    """
    try:
        async with httpx.AsyncClient(timeout=_VALIDATE_TIMEOUT_S) as client:
            if provider == "anthropic":
                resp = await client.get(
                    "https://api.anthropic.com/v1/models",
                    headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                )
                if resp.status_code == 200:
                    return True, ""
                if resp.status_code in (401, 403):
                    return False, "Anthropic rejected this key."
                return None, f"Anthropic answered HTTP {resp.status_code}."
            if provider == "openai":
                resp = await client.get(
                    "https://api.openai.com/v1/models",
                    headers={"Authorization": f"Bearer {key}"},
                )
                if resp.status_code == 200:
                    return True, ""
                if resp.status_code in (401, 403):
                    return False, "OpenAI rejected this key."
                return None, f"OpenAI answered HTTP {resp.status_code}."
            resp = await client.post(
                "https://api.capsolver.com/getBalance", json={"clientKey": key}
            )
            if resp.status_code == 200:
                body = resp.json()
                if body.get("errorId") == 0:
                    return True, ""
                return False, body.get("errorDescription") or "CapSolver rejected this key."
            return None, f"CapSolver answered HTTP {resp.status_code}."
    except httpx.RequestError:
        return None, "Could not reach the provider from this machine."


@setup_router.post("/setup/validate-key")
async def setup_validate_key(payload: _KeyCheck):
    if _configured():
        return JSONResponse({"detail": "Setup is already complete."}, status_code=403)
    ok, reason = await _check_provider_key(payload.provider, payload.key.strip())
    return {"ok": ok, "reason": reason}


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
    dashboard_password = dashboard_password.strip()
    if len(dashboard_password) < MIN_PASSWORD_LENGTH:
        return HTMLResponse(
            f"Dashboard password must be at least {MIN_PASSWORD_LENGTH} characters.",
            status_code=400,
        )
    if not (anthropic_api_key.strip() or openai_api_key.strip()):
        return HTMLResponse(
            "At least one model provider key (Anthropic or OpenAI) is required.",
            status_code=400,
        )
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
