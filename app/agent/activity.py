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
) -> None:
    """Record what a session is doing. ``stream`` carries the full accumulated
    text of a token-by-token phase (model reasoning as it generates); it is
    never a window or a tail slice, and a call that omits it clears any
    previous stream rather than inheriting it, so a phase change (e.g. moving
    to "Running actions") can't leak stale reasoning text into the next read.
    """
    prev = _activity.get(session_id) or {}
    _activity[session_id] = {
        "label": label,
        "startedAt": datetime.now(timezone.utc).isoformat(),
        "step": step if step is not None else prev.get("step"),
        "spin": spin,
        "stream": stream,
    }


_coverage: dict[str, list[dict]] = {}


def set_coverage(session_id: str, items: list[dict]) -> None:
    _coverage[session_id] = items


def get_coverage(session_id: str) -> list[dict] | None:
    return _coverage.get(session_id)


def get_activity(session_id: str) -> dict | None:
    return _activity.get(session_id)


def clear_activity(session_id: str) -> None:
    _activity.pop(session_id, None)
    _coverage.pop(session_id, None)
