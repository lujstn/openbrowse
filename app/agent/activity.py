"""Ephemeral per-session activity state for the live feed.

Holds what a running session is doing *right now* (waiting for the model, acting,
preparing the next step) plus when that phase started, so the dashboard can show a
live indicator with a count-up timer. In-memory only; never persisted.
"""

from __future__ import annotations

from datetime import datetime, timezone


_activity: dict[str, dict] = {}


def set_activity(
    session_id: str,
    label: str,
    step: int | None = None,
    spin: bool = False,
    stream: str | None = None,
    seconds: float | None = None,
) -> None:
    """Record what a session is doing. ``stream`` carries the full accumulated
    text of a token-by-token phase (model reasoning as it generates); it is
    never a window or a tail slice, and a call that omits it clears any
    previous stream rather than inheriting it, so a phase change (e.g. moving
    to "Running actions") can't leak stale reasoning text into the next read.
    """
    prev = _activity.get(session_id) or {}
    now = datetime.now(timezone.utc).isoformat()
    # @nonobvious(must-hold): a streaming phase re-pushes several times a second,
    # so the clock has to survive an unchanged label or it reads near zero forever
    # and no phase can report how long it took.
    started = prev.get("startedAt") if prev.get("label") == label else None
    _activity[session_id] = {
        "label": label,
        "startedAt": started or now,
        "step": step if step is not None else prev.get("step"),
        "spin": spin,
        "stream": stream,
        # @nonobvious(forced-by): a phase whose label was interrupted and restored
        # loses its clock, so a caller that measured the real elapsed time says so
        # here rather than leaving the dashboard to infer it from startedAt.
        "seconds": seconds,
    }


def get_activity(session_id: str) -> dict | None:
    return _activity.get(session_id)


def clear_activity(session_id: str) -> None:
    _activity.pop(session_id, None)
