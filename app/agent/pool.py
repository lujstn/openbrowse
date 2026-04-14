"""Session pool — manages concurrent agent sessions with a hard cap."""

from __future__ import annotations

import asyncio
import logging

from app.agent.runner import run_agent_session
from app.config import settings

logger = logging.getLogger(__name__)


class SessionPool:
    """Concurrency-limited pool for agent sessions."""

    def __init__(self, max_concurrent: int | None = None) -> None:
        self._max = max_concurrent or settings.max_concurrent_sessions
        self._semaphore = asyncio.Semaphore(self._max)
        self._tasks: dict[str, asyncio.Task[None]] = {}

    @property
    def active_count(self) -> int:
        return self._max - self._semaphore._value

    async def submit(self, session_id: str) -> None:
        await self._semaphore.acquire()
        task = asyncio.create_task(self._run_and_release(session_id))
        self._tasks[session_id] = task

    async def _run_and_release(self, session_id: str) -> None:
        try:
            await run_agent_session(session_id)
        except Exception:
            logger.exception("Unhandled error in session %s", session_id)
        finally:
            self._semaphore.release()
            self._tasks.pop(session_id, None)

    async def cancel(self, session_id: str) -> bool:
        task = self._tasks.get(session_id)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            return True
        return False

    async def shutdown(self) -> None:
        for session_id in list(self._tasks):
            await self.cancel(session_id)


pool = SessionPool()
