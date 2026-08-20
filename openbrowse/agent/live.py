"""Live session registry — the handle on sessions whose browser is still running.

A keep-alive session does not end when its task ends: the worker in
``app.agent.runner`` parks with Chrome, the tabs and the agent's own message
history intact, and waits here for the next thing the user says. Every other
part of the app talks to that parked worker through this module: the dashboard
and API deliver follow-ups, the pool evicts a parked session when a new one
needs its display slot, and stop requests ask it to let go.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from openbrowse.db import crud

logger = logging.getLogger(__name__)

DELIVERED = "delivered"
BUSY = "busy"
COLD = "cold"

_REPLAY_TOTAL_CAP = 4000
_REPLAY_ANSWER_CAP = 1200
_REPLAY_REQUEST_CAP = 600


@dataclass
class LiveSession:
    """One running worker: its agent, its follow-up inbox and its release flag."""

    session_id: str
    agent: Any = None
    inbox: asyncio.Queue = field(default_factory=asyncio.Queue)
    release: asyncio.Event = field(default_factory=asyncio.Event)
    finished: asyncio.Event = field(default_factory=asyncio.Event)
    parked_since: datetime | None = None
    release_reason: str = ""

    @property
    def parked(self) -> bool:
        return self.parked_since is not None


_live: dict[str, LiveSession] = {}


def register(session_id: str, agent: Any) -> LiveSession:
    if session_id in _live:
        logger.warning("Session %s registered twice; the older worker is orphaned", session_id)
    entry = LiveSession(session_id=session_id, agent=agent)
    _live[session_id] = entry
    return entry


def unregister(entry: LiveSession | None) -> None:
    """Drop a worker's registration, then wake anything awaiting its teardown.

    Identity-checked: a worker only ever clears its own entry, never a newer
    one registered for the same session after it was released.
    """
    if entry is None:
        return
    if _live.get(entry.session_id) is entry:
        _live.pop(entry.session_id, None)
    entry.finished.set()


def get(session_id: str) -> LiveSession | None:
    return _live.get(session_id)


def get_live_agent(session_id: str) -> Any | None:
    """The running browser-use ``Agent`` for a session, or None. Lets callers use
    the agent's native cooperative ``stop()``/``pause()``/``resume()``.
    """
    entry = _live.get(session_id)
    return entry.agent if entry else None


def is_live(session_id: str) -> bool:
    """Whether the session's browser is still up — running a task or parked."""
    return session_id in _live


def is_parked(session_id: str) -> bool:
    entry = _live.get(session_id)
    return bool(entry and entry.parked)


def park(entry: LiveSession) -> None:
    entry.parked_since = datetime.now(timezone.utc)


def parked_sessions() -> list[LiveSession]:
    """Parked workers, longest-parked first."""
    return sorted(
        (e for e in _live.values() if e.parked),
        key=lambda e: e.parked_since or datetime.now(timezone.utc),
    )


def deliver(session_id: str, text: str) -> str:
    """Hand a follow-up to a parked worker.

    Returns ``DELIVERED`` when the worker took it, ``BUSY`` when the session is
    live but mid-task (so the caller must reject rather than start a second
    run against the same browser), and ``COLD`` when no worker holds this
    session at all and a fresh run is the only way to answer.
    """
    entry = _live.get(session_id)
    if entry is None:
        return COLD
    if not entry.parked or entry.release.is_set():
        return BUSY
    # @nonobvious(must-hold): unparked before the queue put, so a second
    # follow-up racing this one is answered BUSY instead of being delivered
    # into a worker that is already waking up.
    entry.parked_since = None
    entry.inbox.put_nowait(text)
    return DELIVERED


def stop_agent(session_id: str) -> bool:
    """Ask the agent to stop cooperatively at its next step. True if one was asked."""
    entry = _live.get(session_id)
    if entry is None or entry.agent is None:
        return False
    try:
        entry.agent.stop()
        return True
    except Exception:
        logger.warning("agent.stop() failed for %s", session_id, exc_info=True)
        return False


async def request_release(
    session_id: str, reason: str = "", *, wait: bool = True, timeout: float = 30.0
) -> bool:
    """Ask a worker to close its browser and finish. True if one was asked."""
    entry = _live.get(session_id)
    if entry is None:
        return False
    entry.release_reason = reason
    entry.release.set()
    if wait:
        try:
            await asyncio.wait_for(entry.finished.wait(), timeout=timeout)
        except (TimeoutError, asyncio.TimeoutError):
            logger.warning(
                "Session %s did not release within %ss", session_id, timeout
            )
    return True


async def release_idle_slot(reason: str = "") -> bool:
    """Release the longest-parked worker so its display slot can be reused."""
    for entry in parked_sessions():
        released = await request_release(entry.session_id, reason)
        if released:
            return True
    return False


async def release_all(reason: str = "") -> None:
    for session_id in list(_live):
        await request_release(session_id, reason, timeout=10.0)


def _clip(text: str, cap: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= cap else text[: cap - 1].rstrip() + "…"


async def replay_preamble(session_id: str, task: str) -> str:
    """A prompt that re-seeds a follow-up whose browser is already gone.

    A keep-alive session outlives its browser: the idle timeout, an eviction or
    a server restart all release Chrome while the conversation is still open.
    The next follow-up then starts a cold run, and this is the only thing that
    carries the earlier turns into it — each request the user made, paired with
    the answer that was given. Returns "" when there is nothing to replay, which
    is also how the caller knows this is a genuinely new session.
    """
    try:
        messages, _ = await crud.list_messages(session_id, limit=1000)
    except Exception:
        logger.debug("replay preamble message read failed", exc_info=True)
        return ""

    turns: list[tuple[str, str]] = []
    for m in messages:
        mtype = m.get("type")
        if mtype == "user_message":
            turns.append((_clip(m.get("summary") or "", _REPLAY_REQUEST_CAP), ""))
        elif mtype == "completion" and turns:
            answer = ""
            try:
                answer = (json.loads(m.get("data") or "{}") or {}).get("output") or ""
            except (json.JSONDecodeError, TypeError):
                answer = ""
            if not answer:
                answer = m.get("summary") or ""
            turns[-1] = (turns[-1][0], _clip(answer, _REPLAY_ANSWER_CAP))
    turns = [t for t in turns if t[0]]
    # @nonobvious(must-hold): the request being served is already on the feed as
    # an unanswered turn; replaying it here would hand the agent the same words
    # twice, once as history and once as the ask.
    if turns and not turns[-1][1]:
        turns.pop()
    if not turns:
        return ""

    lines: list[str] = []
    for request, answer in reversed(turns):
        block = f"They asked: {request}"
        if answer:
            block += f"\nYou answered: {answer}"
        if sum(len(line) + 1 for line in lines) + len(block) > _REPLAY_TOTAL_CAP:
            break
        lines.append(block)
    body = "\n\n".join(reversed(lines))

    return (
        "EARLIER IN THIS CONVERSATION (the browser from those turns has been "
        "closed, so nothing is open and no login or cookie choice survives — "
        "re-open whatever you need):\n\n"
        f"{body}\n\n"
        "The user is now sending a follow-up to that same conversation. Resolve "
        "any names or pronouns in it from the turns above.\n\nFOLLOW-UP REQUEST:\n"
        f"{task}"
    )
