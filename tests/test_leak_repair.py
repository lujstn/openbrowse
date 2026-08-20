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
    assert ti["evaluation_previous_goal"].startswith("search_page for embed_jid returned 0 matches")
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
    assert ti["memory"].startswith("Still trying to get full untruncated embed_jid UUIDs")
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


def test_html_tags_in_prose_are_not_treated_as_leaks():
    """A model reasoning ABOUT page HTML (<a>, <title>, <script> …) in its thinking
    is prose, not tool-call scaffolding — the scrubber and hoister must leave it
    byte-for-byte intact. Only the card-field tags, <invoke>/<parameter> junk and
    <action> payloads count as leaks.
    """
    from app.agent.leak_repair import scrub_tag_bleed

    prose = (
        "The page has an <a> tag pointing at the job and a <title> element; "
        "the JSON-LD sits in a <script type=\"application/ld+json\"> block. "
        "I should read the <iframe> content next."
    )
    tool_input = {
        "thinking": prose,
        "what_i_see": "A listing with 16 <a> links inside an embed.",
        "action": [{"navigate": {"url": "https://x"}}],
    }
    before = dict(tool_input)
    assert scrub_tag_bleed(tool_input) is False
    assert tool_input == before

    msg = _Msg([_Blk("tool_use", dict(before))])
    assert repair_anthropic_message(msg) == 0
    assert msg.content[0].input["thinking"] == prose


class _NamedBlk:
    def __init__(self, type, input=None, name=None):
        self.type = type
        self.input = input
        self.name = name


def test_merge_action_named_parallel_blocks():
    """Today's observed shape: several tool_use blocks each invoking one action
    by name with that action's bare parameters as the input."""
    msg = _Msg(
        [
            _NamedBlk("thinking"),
            _NamedBlk(
                "tool_use",
                {"key": "detailsPageUrl", "value": "https://example.com/details"},
                name="set_field",
            ),
            _NamedBlk(
                "tool_use", {"frame_url_contains": "ashby"}, name="find_links"
            ),
        ]
    )
    repair_anthropic_message(msg, output_tool_name="AgentOutput")
    first = msg.content[1]
    assert first.input["action"] == [
        {"set_field": {"key": "detailsPageUrl", "value": "https://example.com/details"}},
        {"find_links": {"frame_url_contains": "ashby"}},
    ]


def test_merge_valid_output_block_plus_stragglers():
    """A valid AgentOutput first block must absorb trailing parallel action
    calls instead of silently dropping them."""
    msg = _Msg(
        [
            _NamedBlk(
                "tool_use",
                {"thinking": "go", "action": [{"navigate": {"url": "https://x"}}]},
                name="AgentOutput",
            ),
            _NamedBlk("tool_use", {"name": "rows_draft.json"}, name="add_items_from_file"),
        ]
    )
    repair_anthropic_message(msg, output_tool_name="AgentOutput")
    first = msg.content[0]
    assert first.input["thinking"] == "go"
    assert first.input["action"] == [
        {"navigate": {"url": "https://x"}},
        {"add_items_from_file": {"name": "rows_draft.json"}},
    ]


def test_single_action_named_block_wrapped():
    msg = _Msg(
        [_NamedBlk("tool_use", {"name": "rows_draft.json"}, name="add_items_from_file")]
    )
    repair_anthropic_message(msg, output_tool_name="AgentOutput")
    assert msg.content[0].input["action"] == [
        {"add_items_from_file": {"name": "rows_draft.json"}}
    ]


def test_bare_output_named_blocks_stay_unsalvageable():
    """Blocks named as the output tool with bare params carry no recoverable
    action name — the reply must still fail validation, not guess."""
    msg = _Msg(
        [
            _NamedBlk("tool_use", {"key": "a", "value": "b"}, name="AgentOutput"),
            _NamedBlk("tool_use", {"file_name": "rows.json"}, name="AgentOutput"),
        ]
    )
    repair_anthropic_message(msg, output_tool_name="AgentOutput")
    assert "action" not in msg.content[0].input


def test_single_valid_output_block_untouched():
    msg = _Msg(
        [
            _NamedBlk(
                "tool_use",
                {"thinking": "ok", "action": [{"done": {"success": True}}]},
                name="AgentOutput",
            )
        ]
    )
    repair_anthropic_message(msg, output_tool_name="AgentOutput")
    assert msg.content[0].input["action"] == [{"done": {"success": True}}]


def test_merge_conservative_without_tool_name():
    """With no output tool name known, a lone odd block is left alone."""
    msg = _Msg([_NamedBlk("tool_use", {"name": "x"}, name="add_items_from_file")])
    repair_anthropic_message(msg)
    assert "action" not in msg.content[0].input


def test_mistyped_vs_missing_action_errors():
    from app.agent.leak_repair import mistyped_action_params

    missing = Exception("1 validation error for AgentOutput\naction\n  Field required")
    mistyped = Exception(
        "1 validation error for AgentOutput\n"
        "action.0.read_pages.urls\n"
        "  Input should be a valid list [type=list_type, input_value='[\"a\"]', input_type=str]"
    )
    assert is_missing_action_error(missing)
    assert mistyped_action_params(missing) is None
    assert not is_missing_action_error(mistyped)
    detail = mistyped_action_params(mistyped)
    assert "action.0.read_pages.urls" in detail
    assert "valid list" in detail


def test_coerce_action_param_shapes():
    from app.agent.leak_repair import coerce_action_param_shapes

    kinds = {
        "read_pages": {
            "urls": {"container": "list", "elem": "str", "optional": True},
            "frame_url_contains": {"container": None, "elem": None, "optional": True, "plain_str": True},
        },
        "find_links": {
            "attr": {"container": "dict", "elem": None, "optional": True},
            "container_index": {"container": None, "elem": None, "optional": True},
        },
        "remove_items": {"indices": {"container": "list", "elem": "int", "optional": False}},
        "update_items": {"updates": {"container": "list", "elem": "dict", "optional": False}},
        "navigate": {},
    }
    ti = {
        "thinking": "go",
        "action": [
            {"read_pages": {"urls": '["https://a", "https://b"]', "frame_url_contains": "null"}},
            {"find_links": {"attr": '{"class": "posting"}', "container_index": "null"}},
            {"navigate": {"url": '["not-coerced"]'}},
            {"read_pages": {"urls": "https://single.example/page"}},
            {"remove_items": {"indices": 3}},
            {"update_items": {"updates": {"index": 0, "fields": {"title": "T"}}}},
        ],
    }
    assert coerce_action_param_shapes(ti, kinds) is True
    a = ti["action"]
    assert a[0]["read_pages"]["urls"] == ["https://a", "https://b"]
    assert a[0]["read_pages"]["frame_url_contains"] is None
    assert a[1]["find_links"]["attr"] == {"class": "posting"}
    assert a[1]["find_links"]["container_index"] is None
    assert a[2]["navigate"]["url"] == '["not-coerced"]'
    assert a[3]["read_pages"]["urls"] == ["https://single.example/page"]
    assert a[4]["remove_items"]["indices"] == [3]
    assert a[5]["update_items"]["updates"] == [{"index": 0, "fields": {"title": "T"}}]


def test_coerce_is_noop_on_well_typed_reply():
    import copy

    from app.agent.leak_repair import coerce_action_param_shapes

    kinds = {
        "read_pages": {
            "urls": {"container": "list", "elem": "str", "optional": True},
            "frame_url_contains": {"container": None, "elem": None, "optional": True, "plain_str": True},
        }
    }
    ti = {
        "thinking": "well formed",
        "action": [
            {"read_pages": {"urls": ["https://a"], "frame_url_contains": "ashby"}},
            {"set_field": {"key": "detailsPageUrl", "value": "https://example.org/details"}},
        ],
    }
    before = copy.deepcopy(ti)
    assert coerce_action_param_shapes(ti, kinds) is False
    assert ti == before


def test_merge_drops_unknown_action_names():
    msg = _Msg(
        [
            _NamedBlk("tool_use", {"key": "a", "value": "b"}, name="set_field"),
            _NamedBlk("tool_use", {"x": 1}, name="made_up_tool"),
        ]
    )
    repair_anthropic_message(
        msg, output_tool_name="AgentOutput", action_names={"set_field", "find_links"}
    )
    assert msg.content[0].input["action"] == [
        {"set_field": {"key": "a", "value": "b"}}
    ]
