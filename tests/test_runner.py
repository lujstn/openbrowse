"""Agent runner tests — model resolution."""

from app.agent.runner import _resolve_model, MODEL_MAP


def test_resolve_model_dotted():
    assert _resolve_model("claude-sonnet-4.6") == "claude-sonnet-4-6"


def test_resolve_model_dashed():
    assert _resolve_model("claude-sonnet-4-6") == "claude-sonnet-4-6"


def test_resolve_model_alias():
    assert _resolve_model("bu-max") == "claude-sonnet-4-6"
    assert _resolve_model("bu-ultra") == "claude-opus-4-6"


def test_resolve_model_unknown_passthrough():
    assert _resolve_model("gpt-4o") == "gpt-4o"
