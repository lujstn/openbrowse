"""Tests for the Anthropic action-leak repair (app/agent/leak_repair.py)."""

import json
from pathlib import Path

from app.agent.leak_repair import (
    hoist_leaked_action,
    is_missing_action_error,
    repair_anthropic_message,
    scrub_tag_bleed,
)

_BLEED = json.loads(
    (Path(__file__).parent / "fixtures" / "leak_bleed_run3.json").read_text()
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


_NO_TAGS = (
    "<plan_to_goal",
    "</plan_to_goal",
    "<next_move",
    "</next_move",
    "<evaluation_previous_goal",
    "</evaluation_previous_goal",
    "<memory",
    "</memory",
    "<next_goal",
    "</next_goal",
    "<current_plan_item",
    "<invoke",
    "<parameter",
)


def _assert_no_tags(value: str) -> None:
    for tag in _NO_TAGS:
        assert tag not in value, f"leftover tag {tag!r} in {value!r}"


def test_scrub_step10_well_formed_and_stray_quote_tags():
    """Base tag-bleed: proper <next_move> plus a malformed <evaluation_previous_goal">."""
    ti = {
        "what_i_see": "cookie banner reappeared",
        "plan_to_goal": _BLEED["10"]["plan_to_goal"],
        "thinking": _BLEED["10"]["thinking"],
        "action": [{"click_element_by_index": {"index": 734}}],
    }
    assert scrub_tag_bleed(ti) is True

    assert ti["plan_to_goal"].startswith("Dismiss cookie banner, then try find_elements")
    assert ti["plan_to_goal"].endswith("since it's the actual page loaded.")
    assert ti["next_move"].startswith("Click Reject all to dismiss cookie banner")
    assert ti["evaluation_previous_goal"].startswith("search_page for ashby_jid returned 0 matches")
    assert ti["memory"].startswith("On tab 7ECC viewing Head of Data Science")
    assert ti["next_goal"] == "Dismiss cookie banner then try find_elements selector for apply button hr"
    for key in ("plan_to_goal", "next_move", "evaluation_previous_goal", "memory", "next_goal"):
        _assert_no_tags(ti[key])
    assert ti["what_i_see"] == "cookie banner reappeared"
    assert ti["action"] == [{"click_element_by_index": {"index": 734}}]


def test_scrub_step12_malformed_tags_and_invoke_restart():
    """Step 12 adds the malformed <next_move"> stray-quote tag, a <current_plan_item>,
    and an <invoke ...> tool-call restart leaked into thinking."""
    ti = {
        "what_i_see": "viewing full job description",
        "plan_to_goal": _BLEED["12"]["plan_to_goal"],
        "thinking": _BLEED["12"]["thinking"],
        "action": [{"find_elements": {"selector": "link[rel=canonical]"}}],
    }
    assert scrub_tag_bleed(ti) is True

    assert ti["plan_to_goal"].startswith("Get the canonical URL via find_elements")
    assert ti["plan_to_goal"].endswith("by clicking each in the iframe list.")
    assert ti["next_move"] == (
        "Call find_elements on canonical/og:url tags with attributes to capture "
        "full href content."
    )
    assert ti["evaluation_previous_goal"].startswith("find_elements found 2 matching elements")
    assert ti["memory"].startswith("Still trying to get full untruncated ashby_jid UUIDs")
    assert ti["next_goal"].startswith("Retrieve href/content attribute values")
    assert ti["current_plan_item"] == "3"

    assert ti["thinking"].endswith("untruncated URL.")
    assert "Let me call find_elements again" not in ti["thinking"]
    for key in (
        "plan_to_goal", "next_move", "evaluation_previous_goal", "memory",
        "next_goal", "current_plan_item", "thinking",
    ):
        _assert_no_tags(ti[key])
    assert ti["action"] == [{"find_elements": {"selector": "link[rel=canonical]"}}]


def test_scrub_does_not_overwrite_populated_sibling():
    ti = {"plan_to_goal": _BLEED["10"]["plan_to_goal"], "next_move": "my real next move"}
    assert scrub_tag_bleed(ti) is True
    assert ti["next_move"] == "my real next move"


def test_scrub_noop_on_clean_input():
    ti = {"thinking": "just reasoning", "plan_to_goal": "open the page", "next_move": "click apply"}
    assert scrub_tag_bleed(ti) is False
    assert ti == {"thinking": "just reasoning", "plan_to_goal": "open the page", "next_move": "click apply"}


def test_scrub_leaves_action_list_untouched():
    ti = {"plan_to_goal": _BLEED["12"]["plan_to_goal"], "action": [{"done": {"success": True}}]}
    scrub_tag_bleed(ti)
    assert ti["action"] == [{"done": {"success": True}}]


def test_repair_message_scrubs_and_hoists():
    """The message path both scrubs card-field bleed and hoists a leaked action."""
    msg = _Msg(
        [
            _Blk("text"),
            _Blk(
                "tool_use",
                {
                    "plan_to_goal": _BLEED["12"]["plan_to_goal"],
                    "thinking": 'go <action>[{"navigate": {"url": "https://x"}}]</action>',
                },
            ),
        ]
    )
    assert repair_anthropic_message(msg) == 1
    inp = msg.content[1].input
    assert inp["action"] == [{"navigate": {"url": "https://x"}}]
    assert inp["next_move"].startswith("Call find_elements on canonical/og:url tags")
    _assert_no_tags(inp["plan_to_goal"])
