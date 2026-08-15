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
    assert _resolve_model("bu") == ("anthropic", "claude-sonnet-5")
    assert _resolve_model("bu-latest") == ("anthropic", "claude-sonnet-5")
    assert _resolve_model("bu-ultra") == ("anthropic", "claude-opus-5")
    assert _resolve_model("bu-mini") == ("openai", "gpt-5.6-terra")
    assert _resolve_model("bu-max") == ("openai", "gpt-5.6-sol")


def test_resolve_openai_gpt56():
    with pytest.raises(ValueError, match="not supported"):
        _resolve_model("gpt-5.6-luna")
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
        runner._build_llm("gpt-5.6-terra", "off")


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
    provider, model_id, llm = runner._build_llm("gpt-5.6-terra", "medium")
    assert (provider, model_id) == ("openai", "gpt-5.6-terra")
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
    _, _, llm = runner._build_llm("gpt-5.6-terra", "off")
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


def _cached_state(url: str = "https://x.com/listings"):
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
        return "https://x.com/listings"

    monkeypatch.setattr(runner_mod, "_eval_js", fake_eval)
    flag = {"eligible": True}
    runner_mod._install_lean_state(session, flag)

    stub = await session.get_browser_state_summary()
    assert stub != "FULL"
    assert stub.state_error and "unchanged" in stub.state_error
    assert stub.screenshot is None
    assert stub.dom_state.selector_map == {7: "node"}
    assert stub.url == "https://x.com/listings"
    assert flag["eligible"] is False
    assert calls["full"] == 0

    assert await session.get_browser_state_summary() == "FULL"
    assert calls["full"] == 1


async def test_lean_state_falls_through_on_url_change(monkeypatch):
    import app.agent.runner as runner_mod

    session, calls = _fake_session(_cached_state("https://x.com/listings"))

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


def test_luna_is_refused_outright():
    from app.agent.runner import _resolve_model

    with pytest.raises(ValueError, match="not supported"):
        _resolve_model("gpt-5.6-luna")
    assert _resolve_model("bu-mini") == ("openai", "gpt-5.6-terra")


def test_card_order_puts_action_directly_after_thinking():
    from app.agent.runner import _CARD_ORDER

    assert _CARD_ORDER[0] == "thinking"
    assert _CARD_ORDER[1] == "action"


def test_openai_llm_uses_prompt_schema_not_strict_response_format(monkeypatch):
    import types

    from app.agent import runner as runner_mod

    monkeypatch.setattr(
        runner_mod, "settings", types.SimpleNamespace(openai_api_key="test-key")
    )
    provider, model_id, llm = runner_mod._build_llm("gpt-5.6-terra", "off")
    assert provider == "openai"
    assert model_id == "gpt-5.6-terra"
    assert llm.add_schema_to_system_prompt is True
    assert llm.dont_force_structured_output is True
    assert llm.reasoning_effort == "none"


def _missing_action_exc():
    return ValueError(
        "1 validation error for CardedAgentOutput\naction\n  Field required "
        "[type=missing, input_value={'thinking': 'x'}, input_type=dict]"
    )


async def test_missing_action_retries_twice_then_succeeds(monkeypatch):
    from browser_use import ChatAnthropic

    from app.agent.runner import _RepairingChatAnthropic

    calls: list[list] = []

    async def fake_ainvoke(self, messages, output_format=None, **kwargs):
        calls.append(list(messages))
        if len(calls) < 3:
            raise _missing_action_exc()
        return "ok"

    monkeypatch.setattr(ChatAnthropic, "ainvoke", fake_ainvoke)
    llm = _RepairingChatAnthropic(model="claude-sonnet-5", api_key="k")
    result = await llm.ainvoke(["msg"], output_format=dict)
    assert result == "ok"
    assert len(calls) == 3
    assert len(calls[1]) == 2 and len(calls[2]) == 3
    assert "no executable" in calls[1][1].content
    assert "minimal prose" in calls[2][2].content


async def test_missing_action_three_failures_raises_short_error(monkeypatch):
    import pytest as _pytest

    from browser_use import ChatAnthropic

    from app.agent.runner import _RepairingChatAnthropic

    async def fake_ainvoke(self, messages, output_format=None, **kwargs):
        raise _missing_action_exc()

    monkeypatch.setattr(ChatAnthropic, "ainvoke", fake_ainvoke)
    llm = _RepairingChatAnthropic(model="claude-sonnet-5", api_key="k")
    with _pytest.raises(ValueError) as exc:
        await llm.ainvoke(["msg"], output_format=dict)
    text = str(exc.value)
    assert "abandoned" in text
    assert "validation error" not in text
    assert len(text) < 400
