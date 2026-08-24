"""The headline a finished run gets — the agent's reason, not the reviewer's preamble."""

from openbrowse.agent.runner import _completion_summary
from openbrowse.agent.tools import strip_judge_preamble

_PREAMBLE = (
    'FINAL STRUCTURED OUTPUT (Coverage — tags: 2 item(s).):\n{"title": "x"}\n\n'
    "REVIEW NOTE: URL fields are correct when they resolve to the right page.\n\n"
)
_REASON = "The article body is behind a Sifted Pro subscription modal, so content is partial."


def _summary(done_text: str) -> str:
    return _completion_summary(
        is_successful=False,
        is_done=True,
        raw_success=False,
        schema_valid=True,
        stopped=False,
        done_text=strip_judge_preamble(done_text),
        recovered_errors=0,
    )


def test_agent_reason_survives_the_reviewer_preamble():
    assert _summary(_PREAMBLE + _REASON).startswith("Task failed: The article body is behind")
    assert "FINAL STRUCTURED OUTPUT" not in _summary(_PREAMBLE + _REASON)


def test_plain_done_text_is_left_alone():
    assert strip_judge_preamble(_REASON) == _REASON
    assert _summary(_REASON).startswith("Task failed: The article body is behind")


def test_preamble_without_a_review_note_is_kept_rather_than_guessed_at():
    odd = 'FINAL STRUCTURED OUTPUT (Coverage — tags: 1 item.):\n{"a": 1}\n\nreason'
    assert strip_judge_preamble(odd) == odd


def test_empty_and_missing_text_are_safe():
    assert strip_judge_preamble("") == ""
    assert strip_judge_preamble(None) is None
    assert _summary("") == "Task failed"


def test_a_preamble_only_done_text_leaves_no_reason():
    assert strip_judge_preamble(_PREAMBLE) == ""
    assert _summary(_PREAMBLE) == "Task failed"


def test_success_and_other_endings_are_unchanged():
    assert (
        _completion_summary(
            is_successful=True,
            is_done=True,
            raw_success=True,
            schema_valid=True,
            stopped=False,
            done_text="",
            recovered_errors=0,
        )
        == "Task completed successfully"
    )
    assert _completion_summary(
        is_successful=False,
        is_done=True,
        raw_success=True,
        schema_valid=False,
        stopped=False,
        done_text=_PREAMBLE,
        recovered_errors=0,
    ) == "Task finished but the result did not match the requested schema"
