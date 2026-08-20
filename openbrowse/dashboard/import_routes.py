"""Dashboard routes for the BU Cloud → RPi import flow (fetch → confirm → import).

The token arrives on /start, is used only to drive the fetch, and is never persisted or stored
on the job. Fetched cookie jars are staged in memory and cleared once imported.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from pathlib import Path
from typing import AsyncGenerator

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sse_starlette.sse import EventSourceResponse

from openbrowse.auth import require_dashboard_auth
from openbrowse.profiles.cloud_export import export_cloud_profile, list_cloud_profiles
from openbrowse.profiles.import_jobs import jobs
from openbrowse.profiles.importer import import_bundle
from openbrowse.profiles.storage import cookie_domains

logger = logging.getLogger(__name__)

_template_dir = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_template_dir))

router = APIRouter(tags=["import"], dependencies=[Depends(require_dashboard_auth)])

_tasks: set[asyncio.Task] = set()


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def _run_fetch(job_id: str, token: str) -> None:
    job = jobs.get(job_id)
    if not job:
        return
    try:
        job.phase = "Listing BU Cloud profiles"
        cloud = await list_cloud_profiles(token)
        if not cloud:
            job.status = "error"
            job.error = "No profiles found for this token."
            return
        job.profiles = [
            {
                "id": p["id"],
                "name": p.get("name"),
                "status": "pending",
                "domains": p.get("cookieDomains") or [],
                "cookie_count": None,
                "origin_count": None,
                "progress": None,
            }
            for p in cloud
        ]
        for p in job.profiles:
            p["status"] = "exporting"
            label = p["name"] or p["id"]
            job.phase = f"Exporting {label}"

            def _log(msg: str, _label: str = label) -> None:
                job.phase = f"{_label}: {msg}"

            def _progress(done: int, total: int, _p=p, _label=label) -> None:
                _p["progress"] = {"done": done, "total": total}
                job.phase = f"{_label}: checking sites {done}/{total}"

            try:
                state = await export_cloud_profile(token, p["id"], on_log=_log, on_progress=_progress)
            except Exception as exc:
                logger.warning("Cloud export failed for %s", p["id"], exc_info=True)
                p["status"] = "error"
                p["error"] = str(exc)[:200]
                continue
            job.staged[p["id"]] = state
            p["cookie_count"] = len(state["cookies"])
            p["origin_count"] = len(state["origins"])
            p["domains"] = cookie_domains(state)
            p["status"] = "ready"
        if not job.staged:
            job.status = "error"
            job.error = "Nothing could be exported from any profile."
            return
        job.phase = "Captured"
        job.status = "ready"
    except PermissionError as exc:
        job.status = "error"
        job.error = str(exc)
    except Exception as exc:
        logger.warning("Fetch job %s failed", job_id, exc_info=True)
        job.status = "error"
        job.error = f"Fetch failed: {exc}"[:300]


async def _run_confirm(job_id: str, ids: list[str] | None) -> None:
    job = jobs.get(job_id)
    if not job:
        return
    try:
        job.status = "importing"
        job.phase = "Importing"
        selected = ids or [p["id"] for p in job.profiles if p["id"] in job.staged]
        bundle = []
        for pid in selected:
            state = job.staged.get(pid)
            if not state:
                continue
            name = next((p["name"] for p in job.profiles if p["id"] == pid), None)
            bundle.append({"id": pid, "name": name, **state})
        job.results = await import_bundle(bundle)
        job.staged.clear()
        job.phase = "Done"
        job.status = "done"
    except Exception as exc:
        logger.warning("Import job %s failed", job_id, exc_info=True)
        job.status = "error"
        job.error = f"Import failed: {exc}"[:300]


@router.get("/profiles/import", response_class=HTMLResponse)
async def import_page(request: Request):
    return templates.TemplateResponse(request, "import.html", {})


@router.post("/profiles/import/start")
async def import_start(token: str = Form(...)):
    token = (token or "").strip()
    if not token:
        return JSONResponse({"error": "A BU Cloud API token is required."}, status_code=400)
    job = jobs.new()
    _spawn(_run_fetch(job.id, token))
    return JSONResponse({"jobId": job.id})


@router.get("/profiles/import/{job_id}/events")
async def import_events(request: Request, job_id: str):
    async def gen() -> AsyncGenerator[dict[str, str], None]:
        while True:
            if await request.is_disconnected():
                break
            job = jobs.get(job_id)
            if not job:
                yield {"event": "job", "data": json.dumps({"status": "error", "error": "Job expired or not found."})}
                break
            yield {"event": "job", "data": json.dumps(job.summary())}
            if job.status in ("done", "error"):
                break
            await asyncio.sleep(1)

    return EventSourceResponse(gen())


@router.post("/profiles/import/{job_id}/confirm")
async def import_confirm(request: Request, job_id: str):
    job = jobs.get(job_id)
    if not job:
        return JSONResponse({"error": "Job not found or expired."}, status_code=404)
    if job.status != "ready":
        return JSONResponse({"error": f"Job is not ready (status: {job.status})."}, status_code=409)
    ids: list[str] | None = None
    with contextlib.suppress(Exception):
        body = await request.json()
        if isinstance(body, dict) and isinstance(body.get("ids"), list):
            ids = [str(i) for i in body["ids"]]
    _spawn(_run_confirm(job_id, ids))
    return JSONResponse({"ok": True})


@router.post("/profiles/import/{job_id}/cancel")
async def import_cancel(job_id: str):
    jobs.drop(job_id)
    return JSONResponse({"ok": True})
