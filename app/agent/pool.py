"""Session pool — manages concurrent agent sessions with a hard cap."""

from __future__ import annotations

import asyncio
import logging

from app.agent import live
from app.agent.runner import run_agent_session
from app.config import settings

logger = logging.getLogger(__name__)


class SessionPool:
    """Concurrency-limited pool for agent sessions."""

    def __init__(self, max_concurrent: int | None = None) -> None:
        self._max = max_concurrent or settings.max_concurrent_sessions
        self._semaphore = asyncio.Semaphore(self._max)
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._running: set[str] = set()

    @property
    def active_count(self) -> int:
        return len(self._running)

    @property
    def queued_count(self) -> int:
        return len(self._tasks) - len(self._running)

    def submit_nowait(self, session_id: str) -> None:
        """Queue a session and return immediately; the semaphore gates inside the
        task, so an over-cap submit queues instead of blocking the caller, and a
        cancel while queued raises out of acquire without ever taking a permit.
        """
        task = asyncio.get_running_loop().create_task(self._run_gated(session_id))
        self._tasks[session_id] = task

    async def follow_up(self, session_id: str, text: str) -> str:
        """Hand a follow-up to the session's parked worker, if it still has one.

        ``live.DELIVERED`` means the running agent took it and no new run is
        needed; ``live.BUSY`` means the session is mid-task; ``live.COLD`` means
        its browser is gone and the caller must start a fresh run.
        """
        return live.deliver(session_id, text)

    async def _run_gated(self, session_id: str) -> None:
        try:
            if self.active_count >= self._max:
                # @nonobvious(forced-by): a keep-alive session parked between
                # follow-ups still holds its display slot — without reclaiming
                # it, a one-slot host would leave the new session queued behind
                # a browser nobody is using, forever.
                await live.release_idle_slot(
                    "display slot handed to a newly started session"
                )
            async with self._semaphore:
                self._running.add(session_id)
                try:
                    await run_agent_session(session_id)
                finally:
                    self._running.discard(session_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Unhandled error in session %s", session_id)
        finally:
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
        await live.release_all("server shutting down")
        for session_id in list(self._tasks):
            await self.cancel(session_id)


pool = SessionPool()
