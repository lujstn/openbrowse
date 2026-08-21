"""Unit tests for the live suite's trace parsing and assertions — these run in the
normal (free) suite so the machinery is proven before any money runs through it."""

import json
from pathlib import Path

import pytest

from tests.live.harness import (
    RunTrace,
    assert_no_doom_loop,
    assert_not_used,
    assert_success,
    assert_used,
    count_calls,
    fingerprint,
)


def _msg(mtype: str, summary: str = "", **data) -> dict:
    return {
        "id": f"m{id(data)}",
        "type": mtype,
        "summary": summary,
        "data": json.dumps(data) if data else "",
    }


def _session(**overrides) -> dict:
    base = {
        "status": "stopped",
        "isTaskSuccessful": True,
        "failureKind": None,
        "stepCount": 3,
        "totalCostUsd": "0.0312",
        "output": json.dumps({"items": [{"name": "b"}, {"name": "a"}]}),
    }
    base.update(overrides)
    return base


def _trace(messages, session=None) -> RunTrace:
    return RunTrace.build(session or _session(), messages, Path("/tmp/x.json"))


def test_executed_actions_and_args_are_parsed() -> None:
    fp = fingerprint({"selector": "a"})
    trace = _trace(
        [
            _msg(
                "browser_action",
                action="find_elements",
                actions=["navigate", "find_elements"],
                args=["h1", fp],
            ),
            _msg("result", action="extract", actions=["extract"], args=["h2"]),
        ]
    )
    assert trace.used == {"navigate": 1, "find_elements": 1, "extract": 1}
    assert count_calls(trace, "find_elements", {"selector": "a"}) == 1
    assert count_calls(trace, "find_elements", {"selector": "other"}) == 0


def test_pre_instrumentation_rows_fall_back_to_primary_action() -> None:
    trace = _trace([_msg("browser_action", action="click")])
    assert trace.used == {"click": 1}
    # Blank fingerprints must not read as identical calls.
    trace = _trace([_msg("browser_action", action="click") for _ in range(5)])
    assert trace.max_repeat_run <= 1
    assert_no_doom_loop(trace)


def test_doom_loop_detected_across_consecutive_identical_calls() -> None:
    fp = fingerprint({"selector": "a[href*='twitter.com']"})
    rows = [
        _msg("result", action="find_elements", actions=["find_elements"], args=[fp])
        for _ in range(9)
    ]
    trace = _trace(rows)
    assert trace.max_repeat_run == 9
    with pytest.raises(AssertionError, match="doom loop"):
        assert_no_doom_loop(trace)
    # Different arguments break the streak.
    rows[4] = _msg(
        "result",
        action="find_elements",
        actions=["find_elements"],
        args=[fingerprint({"selector": "a.other"})],
    )
    assert _trace(rows).max_repeat_run == 4


def test_error_rows_key_on_error_action() -> None:
    trace = _trace(
        [
            _msg(
                "browser_action_error",
                action="write_file",
                actions=["write_file", "upload_file"],
                args=["x", "y"],
                error_action="upload_file",
            )
        ]
    )
    assert trace.errored == {"upload_file": 1}


def test_judge_rounds_count_only_change_requests() -> None:
    trace = _trace(
        [
            _msg("event", category="judge", action="review", verdict="changes"),
            _msg("event", category="judge", action="review", verdict="PASS"),
            _msg("event", category="judge", action="storeComplete"),
        ]
    )
    assert trace.judge_rounds == 1


def test_raw_traceback_data_does_not_crash_parsing() -> None:
    trace = _trace(
        [
            {
                "id": "m1",
                "type": "browser_action_error",
                "summary": "boom",
                "data": "Traceback (most recent call last):\n  ...",
            }
        ]
    )
    assert trace.steps == []


def test_assert_success_rejects_budget_salvage_and_judge_rounds() -> None:
    ok = _trace(
        [_msg("completion", summary="Task completed successfully (recovered from 1 transient errors)")]
    )
    assert_success(ok)

    salvaged = _trace(
        [_msg("completion", summary="Task completed successfully")],
        session=_session(failureKind="budget_exceeded"),
    )
    with pytest.raises(AssertionError, match="budget"):
        assert_success(salvaged)

    reviewed = _trace(
        [
            _msg("completion", summary="Task completed successfully"),
            _msg("event", category="judge", action="review", verdict="changes"),
        ]
    )
    with pytest.raises(AssertionError, match="reviewer"):
        assert_success(reviewed)


def test_used_and_not_used_assertions() -> None:
    trace = _trace([_msg("result", action="extract", actions=["extract"], args=["x"])])
    assert_used(trace, "extract")
    assert_not_used(trace, "read_pages")
    with pytest.raises(AssertionError, match="worked around"):
        assert_not_used(trace, "extract")
    with pytest.raises(AssertionError, match="at least"):
        assert_used(trace, "read_pages")


def test_costs_parse_from_strings_and_output_from_json() -> None:
    trace = _trace([], session=_session())
    assert trace.total_cost_usd == pytest.approx(0.0312)
    assert trace.output == {"items": [{"name": "b"}, {"name": "a"}]}
