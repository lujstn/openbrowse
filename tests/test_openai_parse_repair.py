"""The OpenAI reply parser must repair coercible argument shapes exactly as the
Anthropic client does. Without this pass, one slightly mis-shaped argument
hard-fails the reply; the correction reads to the model as a rejection of the
action itself, and it durably concludes the tool does not exist — a live shots
run failed three times on exactly that."""

import json

from browser_use import Tools
from browser_use.agent.views import AgentOutput

from openbrowse.agent.runner import _ResponsesChatOpenAI
from openbrowse.agent.tools import action_param_kinds


def _llm_and_format():
    tools = Tools()
    llm = _ResponsesChatOpenAI(model="gpt-5.6-terra", api_key="test-key")
    llm._action_param_kinds = action_param_kinds(tools)
    out = AgentOutput.type_with_custom_actions(tools.registry.create_action_model())
    return llm, out


def _reply(action: dict) -> str:
    return json.dumps(
        {
            "thinking": "t",
            "evaluation_previous_goal": "e",
            "memory": "m",
            "next_goal": "n",
            "action": [action],
        }
    )


def test_quoted_list_argument_is_coerced_not_rejected() -> None:
    llm, out = _llm_and_format()
    parsed = llm._parse_structured(
        _reply({"find_elements": {"selector": "a", "attributes": '["href"]'}}), out
    )
    dumped = parsed.action[0].model_dump(exclude_none=True)
    assert dumped["find_elements"]["attributes"] == ["href"]


def test_well_typed_reply_is_untouched() -> None:
    llm, out = _llm_and_format()
    parsed = llm._parse_structured(
        _reply({"screenshot": {"file_name": "shot.png"}}), out
    )
    dumped = parsed.action[0].model_dump(exclude_none=True)
    assert dumped["screenshot"]["file_name"] == "shot.png"


def test_correction_text_forbids_concluding_a_tool_is_missing() -> None:
    from openbrowse.agent.runner import _mistyped_correction

    text = _mistyped_correction("action.0.screenshot: Input should be a valid dictionary")
    assert "exists and is valid" in text
    assert "never conclude" in text
    assert "do not drop the action" in text
