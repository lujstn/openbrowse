"""The budget rule is measured, not predicted: at each step boundary the run
stops when spend has reached the cap, or when the remainder cannot cover one
more step at this turn's average step cost. With plenty of headroom the
average test passes trivially, so a healthy run is never throttled."""

from openbrowse.agent.runner import _budget_stop_reason


def test_no_budget_never_stops() -> None:
    assert _budget_stop_reason(None, 5.0, 5.0, 10) is None
    assert _budget_stop_reason(0, 5.0, 5.0, 10) is None


def test_reaching_the_cap_stops_with_the_exceeded_message() -> None:
    reason = _budget_stop_reason(0.25, 0.26, 0.26, 8)
    assert reason and "exceeded budget" in reason


def test_plenty_of_headroom_keeps_going() -> None:
    # $0.04 spent over 4 steps; $0.21 remaining covers many more average steps.
    assert _budget_stop_reason(0.25, 0.04, 0.04, 4) is None


def test_remainder_below_average_step_stops_before_the_step() -> None:
    # $0.23 spent over 10 steps (avg $0.023); $0.02 left cannot cover a step.
    reason = _budget_stop_reason(0.25, 0.23, 0.23, 10)
    assert reason and "cannot cover another step" in reason


def test_remainder_equal_to_average_step_proceeds() -> None:
    # avg $0.02/step, exactly $0.02 remaining: equal means proceed.
    assert _budget_stop_reason(0.25, 0.23, 0.20, 10) is None


def test_first_step_has_no_average_yet() -> None:
    assert _budget_stop_reason(0.25, 0.0, 0.0, 0) is None


def test_carried_spend_counts_toward_the_cap_but_not_the_average() -> None:
    # A follow-up turn: $0.20 carried from earlier turns counts against the
    # cap, but the average reflects only this turn's two cheap steps.
    reason = _budget_stop_reason(0.25, 0.22, 0.02, 2)
    assert reason is None
    reason = _budget_stop_reason(0.25, 0.244, 0.024, 2)
    assert reason and "cannot cover another step" in reason
