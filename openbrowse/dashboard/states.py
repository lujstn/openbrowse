"""Single source of truth for the user-facing session state.

Six states cover every session: queued, running, success (green),
completed with errors (yellow — the run ended early, e.g. at its budget cap,
but the output was complete and stands), stopped (red — capped by the system
or halted by a human before an answer existed), and error (red — a real
failure). The list rows, the detail header and the live-updating JavaScript
must all agree, so they all derive from here; the JS mirror lives in
session_detail.html's displayStatus and must match this mapping.
"""

from __future__ import annotations

from typing import Any


def display_state(
    status: Any, is_task_successful: Any, failure_kind: Any = None
) -> dict[str, str]:
    ok_true = is_task_successful in (1, True)
    ok_false = is_task_successful in (0, False)
    if status == "created":
        return {"key": "queued", "label": "queued"}
    if status == "running":
        return {"key": "running", "label": "running"}
    if status == "error":
        return {"key": "failed", "label": "error"}
    if ok_true and failure_kind:
        return {"key": "attention", "label": "completed with errors"}
    if ok_true:
        return {"key": "completed", "label": "success"}
    if ok_false and failure_kind == "budget_exceeded":
        # Capped with nothing salvageable: stopped, as distinct from erring.
        return {"key": "stopped", "label": "stopped"}
    if ok_false:
        return {"key": "failed", "label": "error"}
    # No verdict at all: interrupted mid-run (human stop, system halt).
    return {"key": "stopped", "label": "stopped"}
