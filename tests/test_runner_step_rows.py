"""The per-step action row the runner persists for each message: executed-only
slicing, argument fingerprints, and error attribution. The live suite's assertions
are built on these keys."""

import json

from openbrowse.agent.runner import _STORE_ONLY_ACTIONS, _executed_actions
from openbrowse.agent.textguard import guard_key


class _FakeAction:
    def __init__(self, payload: dict):
        self._payload = payload

    def model_dump(self, exclude_none: bool = False) -> dict:
        return self._payload


class _FakeModelOutput:
    def __init__(self, *payloads: dict):
        self.action = [_FakeAction(p) for p in payloads]


class _FakeResult:
    def __init__(self, error: str | None = None):
        self.error = error


def _fp(params: dict) -> str:
    return guard_key(json.dumps(params, sort_keys=True, default=str))


def test_executed_slice_drops_requested_but_aborted_actions() -> None:
    mo = _FakeModelOutput(
        {"navigate": {"url": "https://example.com"}},
        {"find_elements": {"selector": "a"}},
        {"extract": {"query": "links"}},
    )
    requested, executed, args, error_action = _executed_actions(mo, [_FakeResult()])
    assert requested == ["navigate", "find_elements", "extract"]
    assert executed == ["navigate"]
    assert args == [_fp({"url": "https://example.com"})]
    assert error_action is None


def test_args_fingerprints_align_with_executed_actions() -> None:
    mo = _FakeModelOutput(
        {"find_elements": {"selector": "a[href]"}},
        {"click": {"index": 3}},
    )
    _, executed, args, _ = _executed_actions(mo, [_FakeResult(), _FakeResult()])
    assert executed == ["find_elements", "click"]
    assert args == [_fp({"selector": "a[href]"}), _fp({"index": 3})]
    # Identical arguments hash identically — the doom-loop detector's premise.
    _, _, again, _ = _executed_actions(
        _FakeModelOutput({"find_elements": {"selector": "a[href]"}}), [_FakeResult()]
    )
    assert again[0] == args[0]


def test_error_attributes_to_the_failing_action_not_the_first() -> None:
    mo = _FakeModelOutput(
        {"write_file": {"file_name": "a.txt", "content": "x"}},
        {"upload_file": {"index": 2, "path": "a.txt"}},
    )
    _, _, _, error_action = _executed_actions(
        mo, [_FakeResult(), _FakeResult(error="no file input")]
    )
    assert error_action == "upload_file"


def test_no_model_output_yields_empty_row() -> None:
    assert _executed_actions(None, None) == ([], [], [], None)


def test_store_only_actions_use_registered_builtin_names() -> None:
    from browser_use import Tools

    builtin = set(Tools().registry.registry.actions)
    assert "replace_file" in builtin
    assert "replace_file" in _STORE_ONLY_ACTIONS
    assert "replace_file_str" not in _STORE_ONLY_ACTIONS
    for name in _STORE_ONLY_ACTIONS & {"read_file", "write_file", "replace_file"}:
        assert name in builtin


def test_full_toolbox_extension_names_every_registered_action() -> None:
    from browser_use import Tools

    from openbrowse.agent.runner import _full_toolbox_extension

    tools = Tools()
    line = _full_toolbox_extension(tools)
    for name in tools.registry.registry.actions:
        assert name in line
    assert "never report a tool as unavailable" in line
