"""Tests for the Anthropic action-leak repair (app/agent/leak_repair.py)."""

from app.agent.leak_repair import (
    hoist_leaked_action,
    is_missing_action_error,
    repair_anthropic_message,
)


def test_basic_leak():
    inp = {
        "thinking": 'need href <action>[{"click_element_by_index": {"index": 11}}]</action>\n</AgentOutput>\n\n'
    }
    assert hoist_leaked_action(inp) is True
    assert inp["action"] == [{"click_element_by_index": {"index": 11}}]
    assert "<action>" not in inp["thinking"] and "AgentOutput" not in inp["thinking"]


def test_nested_brackets_in_string():
    inp = {"thinking": 'x <action>[{"input_text": {"index": 3, "text": "a[1,2]b"}}]</action>'}
    assert hoist_leaked_action(inp) is True
    assert inp["action"] == [{"input_text": {"index": 3, "text": "a[1,2]b"}}]


def test_existing_action_untouched():
    inp = {"thinking": "ok", "action": [{"done": {"success": True}}]}
    assert hoist_leaked_action(inp) is False
    assert inp["action"] == [{"done": {"success": True}}]


def test_no_leak():
    inp = {"thinking": "reasoning only"}
    assert hoist_leaked_action(inp) is False
    assert "action" not in inp


def test_done_bool_parsed():
    inp = {"thinking": 'done <action>[{"done": {"success": true, "text": "x"}}]</action>'}
    assert hoist_leaked_action(inp) is True
    assert inp["action"] == [{"done": {"success": True, "text": "x"}}]


class _Blk:
    def __init__(self, type, input=None):
        self.type = type
        self.input = input


class _Msg:
    def __init__(self, content):
        self.content = content


def test_repair_message():
    msg = _Msg(
        [
            _Blk("text"),
            _Blk("tool_use", {"thinking": 'go <action>[{"navigate": {"url": "https://x"}}]</action>'}),
        ]
    )
    assert repair_anthropic_message(msg) == 1
    assert msg.content[1].input["action"] == [{"navigate": {"url": "https://x"}}]


def test_error_classifier():
    assert is_missing_action_error(
        Exception("1 validation error for AgentOutput\naction\n  Field required")
    )
    assert not is_missing_action_error(Exception("connection reset by peer"))
