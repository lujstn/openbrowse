"""The six user-facing session states, derived in exactly one place. A budget
stop with a standing answer is yellow 'completed with errors'; a stop with
nothing salvageable reads 'stopped'; real errors and unsuccessful outcomes
read 'error'."""

from openbrowse.dashboard.states import display_state


def test_queued() -> None:
    assert display_state("created", None, None) == {"key": "queued", "label": "queued"}


def test_running() -> None:
    assert display_state("running", None, None)["key"] == "running"


def test_completed_success() -> None:
    assert display_state("stopped", 1, None) == {"key": "completed", "label": "success"}
    assert display_state("idle", True, None)["key"] == "completed"


def test_completed_with_warning_is_amber() -> None:
    ds = display_state("stopped", 1, "budget_exceeded")
    assert ds == {"key": "attention", "label": "completed with errors"}


def test_budget_stop_without_answer_reads_stopped_not_error() -> None:
    assert display_state("stopped", 0, "budget_exceeded")["key"] == "stopped"


def test_interrupted_with_no_verdict_is_stopped() -> None:
    assert display_state("stopped", None, None)["key"] == "stopped"


def test_unsuccessful_outcome_is_failed() -> None:
    assert display_state("stopped", 0, None) == {"key": "failed", "label": "error"}
    assert display_state("stopped", 0, "agent_failure")["key"] == "failed"


def test_error_status_is_failed_regardless() -> None:
    assert display_state("error", None, "provider_rate_limit")["key"] == "failed"
