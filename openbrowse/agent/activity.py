"""Ephemeral per-session activity state for the live feed.

Holds what a running session is doing *right now* (waiting for the model, acting,
preparing the next step) plus when that phase started, so the dashboard can show a
live indicator with a count-up timer. Also the run-lifecycle registry: which
sessions are currently running and which profile each has claimed, letting tools
scale contention pacing to real concurrency and letting the runner refuse two
live sessions on one profile. In-memory only; never persisted.
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
    kind: str | None = None,
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
        # @nonobvious(means): "reasoning" marks a phase whose prose is the point.
        # The dashboard shimmers those and spins the rest, so a phase that thinks
        # and a phase that acts do not claim the same affordance.
        "kind": kind,
    }


def get_activity(session_id: str) -> dict | None:
    return _activity.get(session_id)


def clear_activity(session_id: str) -> None:
    _activity.pop(session_id, None)


_running_sessions: set[str] = set()

_claimed_profiles: dict[str, str] = {}


def session_started(session_id: str) -> None:
    _running_sessions.add(session_id)


def session_ended(session_id: str) -> None:
    _running_sessions.discard(session_id)


def active_session_count() -> int:
    return len(_running_sessions)


def try_claim_profile(profile_id: str, session_id: str) -> str | None:
    """Claim a profile for a session. Returns None on success, or the id of the
    session already holding it. Check-and-set with no await between, so a single
    event loop cannot interleave two claims.
    """
    holder = _claimed_profiles.get(profile_id)
    if holder is not None and holder != session_id:
        return holder
    _claimed_profiles[profile_id] = session_id
    return None


def release_profile(profile_id: str, session_id: str) -> None:
    if _claimed_profiles.get(profile_id) == session_id:
        _claimed_profiles.pop(profile_id, None)
