"""FastAPI application — assembles all routers and lifecycle hooks."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.agent.pool import pool
from app.api.profiles import router as profiles_router
from app.api.sessions import router as sessions_router
from app.browser.factory import display_manager
from app.config import settings
from app.dashboard.routes import router as dashboard_router, vnc_router as dashboard_vnc_router
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
    logger.info("Server ready on %s:%d", settings.host, settings.port)
    yield
    logger.info("Shutting down — cancelling sessions and releasing displays...")
    sweeper.cancel()
    try:
        await sweeper
    except asyncio.CancelledError:
        pass
    await pool.shutdown()
    await display_manager.cleanup_all()
    logger.info("Shutdown complete.")


app = FastAPI(
    title="Browser Use Raspberry Pi",
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

app.include_router(sessions_router)
app.include_router(profiles_router)
app.include_router(dashboard_router)
app.include_router(dashboard_vnc_router)


@app.get("/health")
async def health():
    return {"status": "ok", "active_sessions": pool.active_count}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=settings.host, port=settings.port)
