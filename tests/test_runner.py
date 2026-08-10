"""Agent runner tests — model registry, provider routing, thinking, LLM builder."""

import types

import pytest

from app.agent.runner import (
    _OPENAI_REASONING,
    _THINKING_BUDGETS,
    _build_llm,
    _resolve_model,
)


def _fake_settings(*, anthropic: str = "", openai: str = "") -> types.SimpleNamespace:
    return types.SimpleNamespace(
        anthropic_api_key=anthropic,
        openai_api_key=openai,
        default_model="claude-sonnet-5",
    )


def test_resolve_anthropic_dotted_and_dashed():
    assert _resolve_model("claude-sonnet-4.6") == ("anthropic", "claude-sonnet-4-6")
    assert _resolve_model("claude-sonnet-4-6") == ("anthropic", "claude-sonnet-4-6")


def test_resolve_sonnet5_and_opus48():
    assert _resolve_model("claude-sonnet-5") == ("anthropic", "claude-sonnet-5")
    assert _resolve_model("claude-opus-4.8") == ("anthropic", "claude-opus-4-8")
    assert _resolve_model("claude-opus-4-8") == ("anthropic", "claude-opus-4-8")


def test_resolve_aliases():
    assert _resolve_model("bu-max") == ("anthropic", "claude-sonnet-5")
    assert _resolve_model("bu-ultra") == ("anthropic", "claude-opus-4-8")
    assert _resolve_model("bu-mini") == ("openai", "gpt-5.6-luna")


def test_resolve_openai_gpt56():
    assert _resolve_model("gpt-5.6-luna") == ("openai", "gpt-5.6-luna")
    assert _resolve_model("gpt-5.6-terra") == ("openai", "gpt-5.6-terra")
    assert _resolve_model("gpt-5.6-sol") == ("openai", "gpt-5.6-sol")
    assert _resolve_model("gpt-5.6") == ("openai", "gpt-5.6-sol")


def test_resolve_openai_prefix_inference():
    assert _resolve_model("gpt-4o")[0] == "openai"
    assert _resolve_model("o3-mini")[0] == "openai"


def test_resolve_unknown_defaults_anthropic():
    assert _resolve_model("some-future-claude")[0] == "anthropic"


def test_thinking_budget_and_reasoning_maps():
    assert _THINKING_BUDGETS == {"low": 2048, "medium": 8192, "high": 16384}
    assert _OPENAI_REASONING["off"] == "none"
    assert _OPENAI_REASONING["high"] == "high"


def test_build_llm_openai_missing_key(monkeypatch):
    import app.agent.runner as runner

    monkeypatch.setattr(runner, "settings", _fake_settings(openai=""))
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        runner._build_llm("gpt-5.6-luna", "off")


def test_build_llm_anthropic_missing_key(monkeypatch):
    import app.agent.runner as runner

    monkeypatch.setattr(runner, "settings", _fake_settings(anthropic=""))
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        runner._build_llm("claude-sonnet-5", "off")


def test_build_llm_adaptive_thinking(monkeypatch):
    import app.agent.runner as runner

    monkeypatch.setattr(runner, "settings", _fake_settings(anthropic="sk-ant-x"))
    provider, model_id, llm = runner._build_llm("claude-opus-4.8", "high")
    assert (provider, model_id) == ("anthropic", "claude-opus-4-8")
    assert llm.thinking == {"type": "adaptive"}
    assert llm.output_config == {"effort": "high"}


def test_build_llm_budget_thinking_old_model(monkeypatch):
    import app.agent.runner as runner

    monkeypatch.setattr(runner, "settings", _fake_settings(anthropic="sk-ant-x"))
    _, model_id, llm = runner._build_llm("claude-sonnet-4-6", "high")
    assert model_id == "claude-sonnet-4-6"
    assert llm.thinking == {"type": "enabled", "budget_tokens": 16384}
    assert llm.max_tokens == 16384 + 8192


def test_build_llm_anthropic_no_thinking_omits_temperature(monkeypatch):
    import app.agent.runner as runner

    monkeypatch.setattr(runner, "settings", _fake_settings(anthropic="sk-ant-x"))
    _, _, llm = runner._build_llm("claude-sonnet-5", "off")
    assert getattr(llm, "thinking", None) is None
    assert llm.temperature is None


def test_build_llm_openai_reasoning_effort(monkeypatch):
    import app.agent.runner as runner

    monkeypatch.setattr(runner, "settings", _fake_settings(openai="sk-x"))
    provider, model_id, llm = runner._build_llm("gpt-5.6-luna", "medium")
    assert (provider, model_id) == ("openai", "gpt-5.6-luna")
    assert llm.reasoning_effort == "medium"


def test_resolve_opus_1m_suffix_strips_to_base():
    assert _resolve_model("claude-opus-4-8[1m]") == ("anthropic", "claude-opus-4-8")
    assert _resolve_model("claude-opus-5") == ("anthropic", "claude-opus-5")


def test_build_llm_1m_sets_betas_and_stays_adaptive(monkeypatch):
    import app.agent.runner as runner

    monkeypatch.setattr(runner, "settings", _fake_settings(anthropic="sk-ant-x"))
    provider, model_id, llm = runner._build_llm("claude-opus-4-8[1m]", "high")
    assert (provider, model_id) == ("anthropic", "claude-opus-4-8")
    assert llm.betas == [runner.ONE_M_BETA]
    assert llm.thinking == {"type": "adaptive"}


def test_build_llm_opus5_builds(monkeypatch):
    import app.agent.runner as runner

    monkeypatch.setattr(runner, "settings", _fake_settings(anthropic="sk-ant-x"))
    provider, model_id, _ = runner._build_llm("claude-opus-5", "off")
    assert (provider, model_id) == ("anthropic", "claude-opus-5")


def test_openai_subclass_captures_cache_write(monkeypatch):
    import types

    import app.agent.runner as runner

    monkeypatch.setattr(runner, "settings", _fake_settings(openai="sk-x"))
    _, _, llm = runner._build_llm("gpt-5.6-luna", "off")
    details = types.SimpleNamespace(cached_tokens=100, cache_write_tokens=50)
    usage = types.SimpleNamespace(
        prompt_tokens=1000,
        prompt_tokens_details=details,
        completion_tokens=200,
        total_tokens=1200,
    )
    result = llm._get_usage(types.SimpleNamespace(usage=usage))
    assert result.prompt_cached_tokens == 100
    assert result.prompt_cache_creation_tokens == 50
