"""Ephemeral per-session activity state for the live feed.

Holds what a running session is doing *right now* (waiting for the model, acting,
preparing the next step) plus when that phase started, so the dashboard can show a
live indicator with a count-up timer. In-memory only; never persisted.
"""

from __future__ import annotations

from datetime import datetime, timezone


_activity: dict[str, dict] = {}


def set_activity(session_id: str, label: str, step: int | None = None) -> None:
    prev = _activity.get(session_id) or {}
    _activity[session_id] = {
        "label": label,
        "startedAt": datetime.now(timezone.utc).isoformat(),
        "step": step if step is not None else prev.get("step"),
    }


def get_activity(session_id: str) -> dict | None:
    return _activity.get(session_id)


def clear_activity(session_id: str) -> None:
    _activity.pop(session_id, None)
