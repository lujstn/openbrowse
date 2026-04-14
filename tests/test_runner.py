"""Agent runner tests — unit tests for model resolution and cost calculation."""

from app.agent.runner import _resolve_model, _calculate_cost, MODEL_MAP


def test_resolve_model_dotted():
    assert _resolve_model("claude-sonnet-4.6") == "claude-sonnet-4-6"


def test_resolve_model_dashed():
    assert _resolve_model("claude-sonnet-4-6") == "claude-sonnet-4-6"


def test_resolve_model_alias():
    assert _resolve_model("bu-max") == "claude-sonnet-4-6"
    assert _resolve_model("bu-ultra") == "claude-opus-4-6"


def test_resolve_model_unknown_passthrough():
    assert _resolve_model("gpt-4o") == "gpt-4o"


def test_calculate_cost_sonnet():
    cost = _calculate_cost("claude-sonnet-4-6", 1_000_000, 1_000_000)
    assert cost == 18.0


def test_calculate_cost_opus():
    cost = _calculate_cost("claude-opus-4-6", 1_000_000, 1_000_000)
    assert cost == 90.0


def test_calculate_cost_small():
    cost = _calculate_cost("claude-sonnet-4-6", 10_000, 1_000)
    assert abs(cost - 0.045) < 0.001


def test_calculate_cost_unknown_model_uses_default():
    """Unknown model falls back to sonnet pricing."""
    cost = _calculate_cost("unknown-model", 1_000_000, 1_000_000)
    assert cost == 18.0
