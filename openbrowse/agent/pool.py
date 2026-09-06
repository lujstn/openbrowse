"""Session pool — manages concurrent agent sessions with a hard cap."""

from __future__ import annotations

import asyncio
import logging

from openbrowse.agent import live
from openbrowse.agent.runner import run_agent_session
from openbrowse.config import settings

logger = logging.getLogger(__name__)

# Long enough for every worker's bounded teardown, short enough that a service
# manager never waits out its own stop timeout on us.
_SHUTDOWN_TIMEOUT = 45.0


class SessionPool:
    """Concurrency-limited pool for agent sessions."""

    def __init__(self, max_concurrent: int | None = None) -> None:
        self._max = max_concurrent or settings.max_concurrent_sessions
        self._semaphore = asyncio.Semaphore(self._max)
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._workers: set[asyncio.Task[None]] = set()
        self._running: set[asyncio.Task[None]] = set()
        self._cancelling: set[str] = set()

    @property
    def active_count(self) -> int:
        return len(self._running)

    @property
    def queued_count(self) -> int:
        return len(self._workers) - len(self._running)

    def submit_nowait(self, session_id: str) -> None:
        """Queue a session and return immediately; the semaphore gates inside the
        task, so an over-cap submit queues instead of blocking the caller, and a
        cancel while queued raises out of acquire without ever taking a permit.
        """
        task = asyncio.get_running_loop().create_task(self._run_gated(session_id))
        self._workers.add(task)
        # @nonobvious(means): a session can be re-submitted while its previous
        # worker is still unwinding, so this entry names the newest worker — the
        # one a stop should reach — and the older one clears nothing on its way
        # out.
        self._tasks[session_id] = task

    async def follow_up(self, session_id: str, text: str) -> str:
        """Hand a follow-up to the session's parked worker, if it still has one.

        ``live.DELIVERED`` means the running agent took it and no new run is
        needed; ``live.BUSY`` means the session is mid-task; ``live.COLD`` means
        its browser is gone and the caller must start a fresh run.
        """
        return live.deliver(session_id, text)

    async def _run_gated(self, session_id: str) -> None:
        task = asyncio.current_task()
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
                self._running.add(task)
                try:
                    await run_agent_session(session_id)
                finally:
                    self._running.discard(task)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Unhandled error in session %s", session_id)
        finally:
            self._workers.discard(task)
            if self._tasks.get(session_id) is task:
                self._tasks.pop(session_id, None)
                self._cancelling.discard(session_id)

    async def cancel(self, session_id: str) -> bool:
        task = self._tasks.get(session_id)
        if task is None or task.done():
            return False
        # @nonobvious(must-hold): a repeat stop must not cancel the worker a
        # second time. The first cancel is already unwinding it, and a second
        # one lands inside that unwind, abandoning the display slot and profile
        # registration it had yet to hand back.
        if session_id not in self._cancelling:
            self._cancelling.add(session_id)
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.warning("Session %s failed while stopping", session_id, exc_info=True)
        return True

    async def shutdown(self) -> None:
        await live.release_all("server shutting down")
        stopping = [self.cancel(session_id) for session_id in list(self._tasks)]
        if not stopping:
            return
        try:
            await asyncio.wait_for(
                asyncio.gather(*stopping, return_exceptions=True), _SHUTDOWN_TIMEOUT
            )
        except (TimeoutError, asyncio.TimeoutError):
            logger.warning(
                "Sessions still unwinding %.0fs into shutdown; releasing displays anyway",
                _SHUTDOWN_TIMEOUT,
            )


pool = SessionPool()
