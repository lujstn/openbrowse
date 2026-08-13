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


def _cached_state(url: str = "https://x.com/jobs"):
    from browser_use.browser.views import BrowserStateSummary
    from browser_use.dom.views import SerializedDOMState

    return BrowserStateSummary(
        dom_state=SerializedDOMState(_root=None, selector_map={7: "node"}),
        url=url,
        title="Jobs",
        tabs=[],
    )


def _fake_session(cached):
    calls = {"full": 0}

    async def full_fetch(
        include_screenshot=True, cached=False, include_recent_events=False
    ):
        calls["full"] += 1
        return "FULL"

    session = types.SimpleNamespace(
        get_browser_state_summary=full_fetch,
        _cached_browser_state_summary=cached,
    )
    return session, calls


async def test_lean_state_serves_stub_once_then_full(monkeypatch):
    import app.agent.runner as runner_mod

    session, calls = _fake_session(_cached_state())

    async def fake_eval(sess, js):
        return "https://x.com/jobs"

    monkeypatch.setattr(runner_mod, "_eval_js", fake_eval)
    flag = {"eligible": True}
    runner_mod._install_lean_state(session, flag)

    stub = await session.get_browser_state_summary()
    assert stub != "FULL"
    assert stub.state_error and "unchanged" in stub.state_error
    assert stub.screenshot is None
    assert stub.dom_state.selector_map == {7: "node"}
    assert stub.url == "https://x.com/jobs"
    assert flag["eligible"] is False
    assert calls["full"] == 0

    assert await session.get_browser_state_summary() == "FULL"
    assert calls["full"] == 1


async def test_lean_state_falls_through_on_url_change(monkeypatch):
    import app.agent.runner as runner_mod

    session, calls = _fake_session(_cached_state("https://x.com/jobs"))

    async def fake_eval(sess, js):
        return "https://x.com/other"

    monkeypatch.setattr(runner_mod, "_eval_js", fake_eval)
    flag = {"eligible": True}
    runner_mod._install_lean_state(session, flag)
    assert await session.get_browser_state_summary() == "FULL"
    assert calls["full"] == 1


async def test_lean_state_ineligible_passes_through(monkeypatch):
    import app.agent.runner as runner_mod

    session, calls = _fake_session(_cached_state())
    flag = {"eligible": False}
    runner_mod._install_lean_state(session, flag)
    assert await session.get_browser_state_summary() == "FULL"
    assert calls["full"] == 1


def test_lean_state_installs_on_real_browser_session():
    from browser_use import BrowserSession

    import app.agent.runner as runner_mod

    session = BrowserSession(cdp_url="http://127.0.0.1:1")
    runner_mod._install_lean_state(session, {"eligible": False})
    assert session.get_browser_state_summary is not BrowserSession.get_browser_state_summary


def test_store_only_actions_exclude_page_changers():
    from app.agent.runner import _STORE_ONLY_ACTIONS

    for name in ("update_items", "mark_absent", "read_pages", "run_code_file"):
        assert name in _STORE_ONLY_ACTIONS
    for name in ("click", "navigate", "go_to_url", "goto_tab", "open_tabs", "done"):
        assert name not in _STORE_ONLY_ACTIONS


def test_action_detail_and_category_for_new_actions():
    from app.agent.runner import _category_for

    assert _category_for("read_pages") == "read"
    assert _category_for("update_items") == "schema"
    assert _category_for("mark_absent") == "schema"
