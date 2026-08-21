"""Client, trace parsing and assertions for the live tool-coverage suite.

A scenario is one real agent run: create the session over the v3 API, poll it to a
terminal state, page through its message log, and turn that log into a RunTrace the
assertions can interrogate. Every run's full session + messages payload is saved as
an artefact so a failing scenario can be diagnosed without re-spending.
"""

from __future__ import annotations

import json
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from openbrowse.agent.textguard import guard_key

TERMINAL_EXCLUDED = ("running", "created")
POLL_INTERVAL_S = 3.0
DOOM_LOOP_THRESHOLD = 3


def _safe_fromjson(raw: str) -> dict[str, Any]:
    """Message data is usually JSON but fatal-error rows carry a raw traceback."""
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


@dataclass
class StepRow:
    actions: list[str]
    args: list[str]
    msg_type: str
    summary: str
    error_action: str | None
    step: int | None


@dataclass
class RunTrace:
    session: dict[str, Any]
    messages: list[dict[str, Any]]
    artifact_path: Path
    steps: list[StepRow] = field(default_factory=list)
    events: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)
    used: Counter = field(default_factory=Counter)
    errored: Counter = field(default_factory=Counter)
    judge_rounds: int = 0
    max_repeat_run: int = 0
    completion_summary: str = ""

    @classmethod
    def build(
        cls,
        session: dict[str, Any],
        messages: list[dict[str, Any]],
        artifact_path: Path,
    ) -> "RunTrace":
        trace = cls(session=session, messages=messages, artifact_path=artifact_path)
        call_sequence: list[tuple[str, str]] = []
        for msg in messages:
            data = _safe_fromjson(msg.get("data") or "")
            mtype = msg.get("type") or ""
            if mtype in ("browser_action", "browser_action_error", "result"):
                actions = data.get("actions")
                args = data.get("args")
                if not isinstance(actions, list) or not actions:
                    actions = [data["action"]] if data.get("action") else []
                    args = [""] * len(actions)
                if not isinstance(args, list) or len(args) != len(actions):
                    args = [""] * len(actions)
                error_action = data.get("error_action") or (
                    actions[0] if mtype == "browser_action_error" and actions else None
                )
                if not actions and not error_action:
                    # A fatal-error row whose data is a raw traceback, not JSON —
                    # nothing recoverable for assertions.
                    continue
                row = StepRow(
                    actions=list(actions),
                    args=list(args),
                    msg_type=mtype,
                    summary=msg.get("summary") or "",
                    error_action=error_action,
                    step=data.get("step"),
                )
                trace.steps.append(row)
                trace.used.update(row.actions)
                if row.error_action:
                    trace.errored[row.error_action] += 1
                call_sequence.extend(zip(row.actions, row.args))
            elif mtype == "event":
                cat, act = data.get("category") or "", data.get("action") or ""
                trace.events.append((cat, act, data))
                if cat == "judge" and act == "review" and data.get("verdict") == "changes":
                    trace.judge_rounds += 1
            elif mtype == "completion":
                trace.completion_summary = msg.get("summary") or ""

        run = 0
        previous: tuple[str, str] | None = None
        for pair in call_sequence:
            # An empty fingerprint means a pre-instrumentation row; identical
            # blanks must not read as identical calls.
            if pair == previous and pair[1]:
                run += 1
            else:
                run = 1
            trace.max_repeat_run = max(trace.max_repeat_run, run)
            previous = pair

        return trace

    @property
    def is_successful(self) -> bool:
        return self.session.get("isTaskSuccessful") is True

    @property
    def failure_kind(self) -> str | None:
        return self.session.get("failureKind")

    @property
    def total_cost_usd(self) -> float:
        try:
            return float(self.session.get("totalCostUsd") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    @property
    def output(self) -> Any:
        raw = self.session.get("output")
        if isinstance(raw, str) and raw.strip():
            try:
                return json.loads(raw)
            except ValueError:
                return raw
        return raw

    def describe(self) -> str:
        return (
            f"status={self.session.get('status')} success={self.session.get('isTaskSuccessful')} "
            f"failureKind={self.failure_kind} steps={self.session.get('stepCount')} "
            f"cost=${self.total_cost_usd:.4f} used={dict(self.used)} "
            f"errored={dict(self.errored)} judge_rounds={self.judge_rounds} "
            f"max_repeat_run={self.max_repeat_run}\nartefact: {self.artifact_path}"
        )


class LiveClient:
    def __init__(self, base_url: str, api_key: str) -> None:
        self.base_url = base_url.rstrip("/")
        self._http = httpx.Client(
            base_url=self.base_url,
            headers={"X-Browser-Use-API-Key": api_key},
            timeout=30.0,
        )

    def close(self) -> None:
        self._http.close()

    def preflight(self) -> None:
        resp = self._http.get("/v3/sessions", params={"page_size": 1})
        resp.raise_for_status()

    def create_task(self, payload: dict[str, Any]) -> dict[str, Any]:
        resp = self._http.post("/v3/sessions", json=payload)
        resp.raise_for_status()
        return resp.json()

    def get_session(self, session_id: str) -> dict[str, Any]:
        resp = self._http.get(f"/v3/sessions/{session_id}")
        resp.raise_for_status()
        return resp.json()

    def stop(self, session_id: str) -> None:
        try:
            self._http.post(
                f"/v3/sessions/{session_id}/stop", json={"strategy": "session"}
            )
        except httpx.HTTPError:
            pass

    def poll_until_terminal(self, session_id: str, timeout_s: float) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_s
        session = self.get_session(session_id)
        while session.get("status") in TERMINAL_EXCLUDED:
            if time.monotonic() > deadline:
                raise TimeoutError(
                    f"session {session_id} still {session.get('status')} after {timeout_s}s"
                )
            time.sleep(POLL_INTERVAL_S)
            session = self.get_session(session_id)
        return session

    def fetch_all_messages(self, session_id: str) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        after: str | None = None
        while True:
            params: dict[str, Any] = {"limit": 1000}
            if after:
                params["after"] = after
            resp = self._http.get(f"/v3/sessions/{session_id}/messages", params=params)
            resp.raise_for_status()
            payload = resp.json()
            batch = payload.get("messages") or []
            messages.extend(batch)
            if not payload.get("hasMore") or not batch:
                return messages
            after = batch[-1]["id"]

    def run_scenario(
        self,
        *,
        name: str,
        task: str,
        model: str,
        reasoning_effort: str,
        artifact_dir: Path,
        output_schema: dict[str, Any] | None = None,
        max_cost_usd: float = 0.10,
        timeout_s: float = 360.0,
    ) -> RunTrace:
        payload: dict[str, Any] = {
            "task": task,
            "model": model,
            "reasoningEffort": reasoning_effort,
            "maxCostUsd": max_cost_usd,
        }
        if output_schema is not None:
            payload["outputSchema"] = output_schema
        created = self.create_task(payload)
        session_id = created["id"]
        try:
            session = self.poll_until_terminal(session_id, timeout_s)
        finally:
            self.stop(session_id)
        messages = self.fetch_all_messages(session_id)

        artifact_dir.mkdir(parents=True, exist_ok=True)
        artifact_path = artifact_dir / f"{name}-{model}.json"
        artifact_path.write_text(
            json.dumps({"session": session, "messages": messages}, indent=1)
        )
        return RunTrace.build(session, messages, artifact_path)


def fingerprint(params: dict[str, Any]) -> str:
    """The same fingerprint the runner writes for an executed action's arguments."""
    return guard_key(json.dumps(params, sort_keys=True, default=str))


def count_calls(trace: RunTrace, action: str, params: dict[str, Any] | None = None) -> int:
    """How many executed calls of `action` there were — optionally only those whose
    arguments exactly match `params` (compared via the runner's fingerprint)."""
    wanted = fingerprint(params) if params is not None else None
    n = 0
    for row in trace.steps:
        for name, arg in zip(row.actions, row.args):
            if name == action and (wanted is None or arg == wanted):
                n += 1
    return n


# ── Assertions ───────────────────────────────────────────────────────────────


def assert_used(trace: RunTrace, action: str, at_least: int = 1) -> None:
    assert trace.used[action] >= at_least, (
        f"expected '{action}' to be used at least {at_least}x, "
        f"saw {trace.used[action]}x.\n{trace.describe()}"
    )


def assert_not_used(trace: RunTrace, *actions: str) -> None:
    offenders = {a: trace.used[a] for a in actions if trace.used[a]}
    assert not offenders, (
        f"expected none of {actions} to be used (worked around the target tool?), "
        f"saw {offenders}.\n{trace.describe()}"
    )


def assert_no_doom_loop(trace: RunTrace, threshold: int = DOOM_LOOP_THRESHOLD) -> None:
    assert trace.max_repeat_run < threshold, (
        f"doom loop: an identical call (same action, same arguments) ran "
        f"{trace.max_repeat_run}x consecutively (threshold {threshold}).\n{trace.describe()}"
    )


def assert_tool_clean(trace: RunTrace, action: str) -> None:
    assert trace.errored[action] == 0, (
        f"'{action}' errored {trace.errored[action]}x.\n{trace.describe()}"
    )


def assert_success(trace: RunTrace) -> None:
    assert trace.is_successful, f"run did not succeed.\n{trace.describe()}"
    assert trace.failure_kind is None, (
        f"run 'succeeded' but with failureKind={trace.failure_kind} "
        f"(e.g. budget-exceeded salvage).\n{trace.describe()}"
    )
    assert trace.completion_summary.startswith("Task completed successfully"), (
        f"unexpected completion summary: {trace.completion_summary!r}.\n{trace.describe()}"
    )
    assert trace.judge_rounds == 0, (
        f"the built-in reviewer had to intervene {trace.judge_rounds}x — the first "
        f"attempt was not correct.\n{trace.describe()}"
    )


def assert_cost_under(trace: RunTrace, usd: float) -> None:
    assert trace.total_cost_usd < usd, (
        f"run cost ${trace.total_cost_usd:.4f}, ceiling ${usd:.2f}.\n{trace.describe()}"
    )


def _normalise(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _normalise(v) for k, v in sorted(value.items())}
    if isinstance(value, list):
        items = [_normalise(v) for v in value]
        try:
            return sorted(items, key=lambda x: json.dumps(x, sort_keys=True, default=str))
        except TypeError:
            return items
    return value


def assert_output(trace: RunTrace, expected: Any) -> None:
    """Deep, array-order-insensitive comparison against fixture ground truth."""
    actual = trace.output
    assert _normalise(actual) == _normalise(expected), (
        "output does not match ground truth.\n"
        f"expected: {json.dumps(_normalise(expected), indent=1, default=str)}\n"
        f"actual:   {json.dumps(_normalise(actual), indent=1, default=str)}\n"
        f"{trace.describe()}"
    )
