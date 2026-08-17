"""FastAPI application — assembles all routers and lifecycle hooks."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from contextlib import asynccontextmanager

# @nonobvious(forced-by): browser-use reads this env var ONCE at module import to
# set its global per-action timeout (default 180s), which cancelled long
# read_pages calls mid-flight; must be set before any browser_use import and
# stay above read_pages' own 420s budget and below the 520s step_timeout.
os.environ.setdefault("BROWSER_USE_ACTION_TIMEOUT_S", "480")

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app import system_metrics
from app.agent.pool import pool
from app.api.profiles import router as profiles_router
from app.api.sessions import router as sessions_router
from app.browser.factory import display_manager
from app.config import settings
from app.dashboard.import_routes import router as import_router
from app.dashboard.routes import router as dashboard_router, vnc_router as dashboard_vnc_router
from app.dashboard.setup_routes import setup_router
from app.db import crud
from app.db.models import init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def _stale_session_sweeper() -> None:
    while True:
        try:
            await asyncio.sleep(settings.reconcile_interval_seconds)
            expired = await crud.expire_stale_sessions(settings.stale_session_minutes)
            if expired:
                logger.info("Expired %d stale session shell(s)", expired)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Stale-session sweeper failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Initializing database...")
    await init_db()

    interrupted = await crud.reconcile_interrupted_sessions()
    if interrupted:
        logger.warning("Reconciled %d session(s) interrupted by restart", interrupted)
    expired = await crud.expire_stale_sessions(settings.stale_session_minutes)
    if expired:
        logger.info("Expired %d stale session shell(s)", expired)

    sweeper = asyncio.create_task(_stale_session_sweeper())
    metrics_sampler = asyncio.create_task(system_metrics.sampler_loop())
    logger.info("Server ready on %s:%d", settings.host, settings.port)
    yield
    logger.info("Shutting down — cancelling sessions and releasing displays...")
    sweeper.cancel()
    metrics_sampler.cancel()
    for task in (sweeper, metrics_sampler):
        try:
            await task
        except asyncio.CancelledError:
            pass
    await pool.shutdown()
    await display_manager.cleanup_all()
    logger.info("Shutdown complete.")


app = FastAPI(
    title="OpenBrowse",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount(
    "/static",
    StaticFiles(directory=str(Path(__file__).parent / "dashboard" / "static")),
    name="static",
)
app.include_router(sessions_router)
app.include_router(profiles_router)
app.include_router(import_router)
app.include_router(setup_router)
app.include_router(dashboard_router)
app.include_router(dashboard_vnc_router)


@app.get("/health")
async def health():
    return {"status": "ok", "active_sessions": pool.active_count}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=settings.host, port=settings.port)
