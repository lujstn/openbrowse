"""FastAPI application — assembles all routers and lifecycle hooks."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.agent.pool import pool
from app.api.profiles import router as profiles_router
from app.api.sessions import router as sessions_router
from app.browser.factory import display_manager
from app.config import settings
from app.dashboard.routes import router as dashboard_router
from app.db.models import init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Initializing database...")
    await init_db()
    logger.info("Server ready on %s:%d", settings.host, settings.port)
    yield
    logger.info("Shutting down — cancelling sessions and releasing displays...")
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


@app.get("/health")
async def health():
    return {"status": "ok", "active_sessions": pool.active_count}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=settings.host, port=settings.port)
