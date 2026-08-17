"""Agent runner tests — model registry, provider routing, reasoning, LLM builder."""

import types
from datetime import datetime, timezone

import pytest

from app.agent.runner import (
    _THINKING_BUDGETS,
    _build_llm,
    _canonical_stored_effort,
    _resolve_model,
    resolve_default_effort,
    valid_efforts,
    validate_effort,
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


def test_resolve_openai_gpt56():
    assert _resolve_model("gpt-5.6-terra") == ("openai", "gpt-5.6-terra")
    assert _resolve_model("gpt-5.6-sol") == ("openai", "gpt-5.6-sol")
    assert _resolve_model("gpt-5.6-luna") == ("openai", "gpt-5.6-luna")


def test_resolve_unknown_models_rejected():
    for bad in ("gpt-4o", "o3-mini", "some-future-claude", "gpt-5.6", ""):
        with pytest.raises(ValueError, match="not a valid model"):
            _resolve_model(bad)


def test_removed_aliases_rejected():
    for alias in ("bu", "bu-latest", "bu-ultra", "bu-mini", "bu-max"):
        with pytest.raises(ValueError, match="not a valid model"):
            _resolve_model(alias)


def test_model_warnings_removed():
    import app.agent.runner as runner

    assert not hasattr(runner, "_MODEL_WARNINGS")
    assert not hasattr(runner, "_ALWAYS_THINKING_NOTE")


def test_thinking_budget_map():
    assert _THINKING_BUDGETS == {"low": 2048, "medium": 8192, "high": 16384}


def test_build_llm_openai_missing_key(monkeypatch):
    import app.agent.runner as runner

    monkeypatch.setattr(runner, "settings", _fake_settings(openai=""))
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        runner._build_llm("gpt-5.6-terra", "none")


def test_build_llm_anthropic_missing_key(monkeypatch):
    import app.agent.runner as runner

    monkeypatch.setattr(runner, "settings", _fake_settings(anthropic=""))
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        runner._build_llm("claude-sonnet-5", "none")


def test_build_llm_adaptive_thinking(monkeypatch):
    import app.agent.runner as runner

    monkeypatch.setattr(runner, "settings", _fake_settings(anthropic="sk-ant-x"))
    provider, model_id, llm = runner._build_llm("claude-opus-4.8", "high")
    assert (provider, model_id) == ("anthropic", "claude-opus-4-8")
    assert llm.thinking == {"type": "adaptive", "display": "summarized"}
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
    _, _, llm = runner._build_llm("claude-sonnet-5", "none")
    assert llm.thinking == {"type": "disabled"}
    assert llm.temperature is None


def test_build_llm_openai_reasoning_effort(monkeypatch):
    import app.agent.runner as runner

    monkeypatch.setattr(runner, "settings", _fake_settings(openai="sk-x"))
    provider, model_id, llm = runner._build_llm("gpt-5.6-terra", "medium")
    assert (provider, model_id) == ("openai", "gpt-5.6-terra")
    assert llm.reasoning_effort == "medium"


def test_resolve_fable_and_mythos():
    assert _resolve_model("claude-fable-5") == ("anthropic", "claude-fable-5")
    assert _resolve_model("claude-mythos-5") == ("anthropic", "claude-mythos-5")


def test_build_llm_always_thinking_models_reject_none(monkeypatch):
    import app.agent.runner as runner

    monkeypatch.setattr(runner, "settings", _fake_settings(anthropic="sk-ant-x"))
    for model in ("claude-fable-5", "claude-mythos-5"):
        with pytest.raises(ValueError, match="reasoning cannot be disabled"):
            runner._build_llm(model, "none")
        _, _, llm = runner._build_llm(model, "high")
        assert llm.thinking == {"type": "adaptive", "display": "summarized"}
        assert llm.output_config == {"effort": "high"}


def test_always_thinking_models_are_registered():
    import app.agent.runner as runner

    for model in ("claude-fable-5", "claude-mythos-5"):
        assert model in runner._ANTHROPIC_MODELS.values()
        spec = runner._MODEL_REASONING[model]
        assert spec.can_disable is False
        assert "none" not in valid_efforts(model)


def test_every_model_is_priced():
    import app.agent.runner as runner
    from app.agent import cost

    now = datetime(2026, 8, 15, tzinfo=timezone.utc)
    for model_id in set(runner._ANTHROPIC_MODELS.values()) | set(runner._OPENAI_MODELS.values()):
        assert cost._lookup(model_id, now) is not None, model_id


def test_validate_effort_semantics():
    assert validate_effort("claude-sonnet-5", "HIGH") == "high"
    assert validate_effort("claude-sonnet-5", None) == "default"
    assert validate_effort("claude-sonnet-5", "") == "default"
    assert validate_effort("gpt-5.6-terra", "xhigh") == "xhigh"
    assert validate_effort("gpt-5.6-terra", "max") == "max"
    assert validate_effort("gpt-5.6-terra", "none") == "none"
    for model, bad in (
        ("claude-sonnet-5", "minimal"),
        ("claude-sonnet-5", "off"),
        ("claude-sonnet-4-6", "xhigh"),
        ("claude-sonnet-4-6", "max"),
        ("gpt-5.6-terra", "off"),
    ):
        with pytest.raises(ValueError, match="not a valid reasoning effort"):
            validate_effort(model, bad)
    for model in ("claude-fable-5", "claude-mythos-5"):
        with pytest.raises(ValueError, match="reasoning cannot be disabled"):
            validate_effort(model, "none")


def test_canonical_stored_effort_maps_legacy_off():
    assert _canonical_stored_effort("off") == "none"
    assert _canonical_stored_effort("OFF") == "none"
    assert _canonical_stored_effort(None) == "default"
    assert _canonical_stored_effort("") == "default"
    assert _canonical_stored_effort("high") == "high"


def test_resolve_default_effort_per_generation():
    assert resolve_default_effort("claude-sonnet-5") == "high"
    assert resolve_default_effort("claude-opus-5") == "high"
    assert resolve_default_effort("claude-fable-5") == "high"
    assert resolve_default_effort("claude-opus-4-8") == "none"
    assert resolve_default_effort("claude-sonnet-4-6") == "none"
    assert resolve_default_effort("gpt-5.6-terra") == "medium"
    assert resolve_default_effort("gpt-5.6-sol") == "medium"


def test_registry_covers_every_model():
    import app.agent.runner as runner

    all_ids = set(runner._ANTHROPIC_MODELS.values()) | set(runner._OPENAI_MODELS.values())
    assert all_ids == set(runner._MODEL_REASONING)


def test_build_llm_wire_shapes(monkeypatch):
    import app.agent.runner as runner

    monkeypatch.setattr(runner, "settings", _fake_settings(anthropic="sk-ant-x", openai="sk-x"))

    _, _, llm = runner._build_llm("claude-opus-5", "none")
    assert llm.thinking == {"type": "disabled"}
    _, _, llm = runner._build_llm("claude-sonnet-4-6", "none")
    assert llm.thinking == {"type": "disabled"}
    for model in ("claude-sonnet-5", "claude-fable-5", "claude-opus-4-8", "claude-sonnet-4-6"):
        _, _, llm = runner._build_llm(model, "default")
        assert getattr(llm, "thinking", None) is None
        assert getattr(llm, "output_config", None) is None
    _, _, llm = runner._build_llm("claude-sonnet-5", "max")
    assert llm.thinking == {"type": "adaptive", "display": "summarized"}
    assert llm.output_config == {"effort": "max"}

    _, _, llm = runner._build_llm("gpt-5.6-terra", "none")
    assert llm.reasoning_effort == "none"
    _, _, llm = runner._build_llm("gpt-5.6-terra", "default")
    assert llm.reasoning_effort is None
    _, _, llm = runner._build_llm("gpt-5.6-terra", "xhigh")
    assert llm.reasoning_effort == "xhigh"
    _, _, llm = runner._build_llm("gpt-5.6-terra", "max")
    assert llm.reasoning_effort == "max"


def test_build_llm_openai_output_budget_scales_with_effort(monkeypatch):
    import app.agent.runner as runner

    monkeypatch.setattr(runner, "settings", _fake_settings(openai="sk-x"))
    for effort, budget in (
        ("none", 4096),
        ("default", 12288),
        ("low", 8192),
        ("medium", 12288),
        ("high", 16384),
        ("xhigh", 24576),
        ("max", 32768),
    ):
        _, _, llm = runner._build_llm("gpt-5.6-terra", effort)
        assert llm.max_completion_tokens == budget, effort


def test_build_llm_openai_timeout_scales_with_effort(monkeypatch):
    import app.agent.runner as runner

    monkeypatch.setattr(runner, "settings", _fake_settings(openai="sk-x"))
    for effort, timeout in (
        ("none", 90),
        ("default", 90),
        ("low", 90),
        ("medium", 90),
        ("high", 240),
        ("xhigh", 240),
        ("max", 240),
    ):
        _, _, llm = runner._build_llm("gpt-5.6-terra", effort)
        assert llm.timeout == timeout, effort


def test_resolve_opus_1m_suffix_strips_to_base():
    assert _resolve_model("claude-opus-4-8[1m]") == ("anthropic", "claude-opus-4-8")
    assert _resolve_model("claude-opus-5") == ("anthropic", "claude-opus-5")


def test_build_llm_1m_sets_betas_and_stays_adaptive(monkeypatch):
    import app.agent.runner as runner

    monkeypatch.setattr(runner, "settings", _fake_settings(anthropic="sk-ant-x"))
    provider, model_id, llm = runner._build_llm("claude-opus-4-8[1m]", "high")
    assert (provider, model_id) == ("anthropic", "claude-opus-4-8")
    assert llm.betas == [runner.ONE_M_BETA]
    assert llm.thinking == {"type": "adaptive", "display": "summarized"}


def test_build_llm_opus5_builds(monkeypatch):
    import app.agent.runner as runner

    monkeypatch.setattr(runner, "settings", _fake_settings(anthropic="sk-ant-x"))
    provider, model_id, _ = runner._build_llm("claude-opus-5", "none")
    assert (provider, model_id) == ("anthropic", "claude-opus-5")


def _responses_llm(monkeypatch, effort="max"):
    import app.agent.runner as runner

    monkeypatch.setattr(runner, "settings", _fake_settings(openai="sk-x"))
    _, _, llm = runner._build_llm("gpt-5.6-terra", effort)
    return llm


def _messages():
    from browser_use.llm import SystemMessage, UserMessage

    return [SystemMessage(content="be helpful"), UserMessage(content="hello")]


def test_responses_request_shape_explicit_effort(monkeypatch):
    llm = _responses_llm(monkeypatch, "max")
    params = llm._build_request(_messages(), None)
    assert params["model"] == "gpt-5.6-terra"
    assert params["reasoning"] == {"effort": "max", "summary": "auto"}
    assert params["store"] is False
    assert params["max_output_tokens"] == 32768
    assert "tools" not in params
    assert "temperature" not in params
    assert "frequency_penalty" not in params


def test_responses_request_shape_default_omits_reasoning(monkeypatch):
    llm = _responses_llm(monkeypatch, "default")
    params = llm._build_request(_messages(), None)
    assert "reasoning" not in params
    assert params["store"] is False


def test_responses_request_appends_schema_to_system_prompt(monkeypatch):
    from pydantic import BaseModel

    class Out(BaseModel):
        answer: str

    llm = _responses_llm(monkeypatch, "low")
    params = llm._build_request(_messages(), Out)
    system = params["input"][0]
    assert system["role"] == "system"
    assert "<json_schema>" in str(system["content"])
    assert "text" not in params


def _fake_response(
    *,
    output_text="ok",
    status="completed",
    incomplete_reason=None,
    input_tokens=100,
    cached_tokens=40,
    output_tokens=20,
    reasoning_summary=None,
):
    output = []
    if reasoning_summary:
        output.append(
            types.SimpleNamespace(
                type="reasoning",
                summary=[types.SimpleNamespace(type="summary_text", text=reasoning_summary)],
            )
        )
    return types.SimpleNamespace(
        output_text=output_text,
        output=output,
        status=status,
        incomplete_details=(
            types.SimpleNamespace(reason=incomplete_reason) if incomplete_reason else None
        ),
        usage=types.SimpleNamespace(
            input_tokens=input_tokens,
            input_tokens_details=types.SimpleNamespace(cached_tokens=cached_tokens),
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
        ),
    )


def _patch_client(monkeypatch, llm, response):
    captured = {}

    class FakeResponses:
        async def create(self, **kwargs):
            captured.update(kwargs)
            return response

    fake_client = types.SimpleNamespace(responses=FakeResponses())
    monkeypatch.setattr(type(llm), "get_client", lambda self: fake_client)
    return captured


async def test_responses_ainvoke_maps_usage(monkeypatch):
    llm = _responses_llm(monkeypatch, "medium")
    captured = _patch_client(monkeypatch, llm, _fake_response())
    result = await llm.ainvoke(_messages())
    assert result.completion == "ok"
    assert result.usage.prompt_tokens == 100
    assert result.usage.prompt_cached_tokens == 40
    assert result.usage.prompt_cache_creation_tokens is None
    assert result.usage.completion_tokens == 20
    assert captured["reasoning"] == {"effort": "medium", "summary": "auto"}
    assert captured["store"] is False
    assert "tools" not in captured


async def test_responses_ainvoke_truncation_raises(monkeypatch):
    from browser_use.llm.exceptions import ModelOutputTruncatedError

    llm = _responses_llm(monkeypatch, "low")
    _patch_client(
        monkeypatch,
        llm,
        _fake_response(status="incomplete", incomplete_reason="max_output_tokens"),
    )
    with pytest.raises(ModelOutputTruncatedError):
        await llm.ainvoke(_messages())


async def test_responses_ainvoke_parses_structured_output(monkeypatch):
    from pydantic import BaseModel

    class Out(BaseModel):
        answer: str

    llm = _responses_llm(monkeypatch, "low")
    _patch_client(monkeypatch, llm, _fake_response(output_text='{"answer": "42"}'))
    result = await llm.ainvoke(_messages(), output_format=Out)
    assert result.completion.answer == "42"


def test_openai_llm_uses_prompt_schema_not_strict_response_format(monkeypatch):
    from app.agent import runner as runner_mod

    monkeypatch.setattr(
        runner_mod, "settings", types.SimpleNamespace(openai_api_key="test-key")
    )
    provider, model_id, llm = runner_mod._build_llm("gpt-5.6-terra", "none")
    assert provider == "openai"
    assert model_id == "gpt-5.6-terra"
    assert llm.add_schema_to_system_prompt is True
    assert llm.dont_force_structured_output is True
    assert llm.reasoning_effort == "none"


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


def test_card_order_puts_action_directly_after_thinking():
    from app.agent.runner import _CARD_ORDER

    assert _CARD_ORDER[0] == "thinking"
    assert _CARD_ORDER[1] == "action"


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


def test_responses_request_omits_summary_at_none(monkeypatch):
    llm = _responses_llm(monkeypatch, "none")
    params = llm._build_request(_messages(), None)
    assert params["reasoning"] == {"effort": "none"}


async def test_responses_ainvoke_captures_reasoning_summary(monkeypatch):
    llm = _responses_llm(monkeypatch, "high")
    _patch_client(
        monkeypatch, llm, _fake_response(reasoning_summary="**Plan** figure out the page")
    )
    result = await llm.ainvoke(_messages())
    assert result.completion == "ok"
    assert llm._last_model_reasoning == "**Plan** figure out the page"


async def test_responses_summary_400_falls_back_and_remembers(monkeypatch):
    llm = _responses_llm(monkeypatch, "medium")
    from openai import APIStatusError
    import httpx

    calls = []

    class FakeResponses:
        async def create(self, **kwargs):
            calls.append(kwargs)
            if "summary" in (kwargs.get("reasoning") or {}):
                raise APIStatusError(
                    "reasoning.summary requires org verification",
                    response=httpx.Response(
                        400, request=httpx.Request("POST", "http://x")
                    ),
                    body=None,
                )
            return _fake_response()

    fake_client = types.SimpleNamespace(responses=FakeResponses())
    monkeypatch.setattr(type(llm), "get_client", lambda self: fake_client)
    result = await llm.ainvoke(_messages())
    assert result.completion == "ok"
    assert llm._summary_unsupported is True
    assert len(calls) == 2 and "summary" not in calls[1]["reasoning"]
    calls.clear()
    await llm.ainvoke(_messages())
    assert len(calls) == 1 and "summary" not in calls[0]["reasoning"]


async def test_responses_tolerant_parse_handles_trailing_prose(monkeypatch):
    from pydantic import BaseModel

    class Out(BaseModel):
        answer: str

    llm = _responses_llm(monkeypatch, "low")
    for text in (
        '{"answer": "42"} And that is my final answer.',
        'Here you go:\n```json\n{"answer": "42"}\n```',
        '{"answer": "42"}{"echo": true}',
    ):
        _patch_client(monkeypatch, llm, _fake_response(output_text=text))
        result = await llm.ainvoke(_messages(), output_format=Out)
        assert result.completion.answer == "42", text


async def test_responses_tolerant_parse_still_raises_on_junk(monkeypatch):
    from pydantic import BaseModel

    class Out(BaseModel):
        answer: str

    llm = _responses_llm(monkeypatch, "low")
    _patch_client(monkeypatch, llm, _fake_response(output_text="no json here at all"))
    with pytest.raises(Exception):
        await llm.ainvoke(_messages(), output_format=Out)


def _missing_action_response(n_failures: int):
    good = '{"action": "click"}'
    bad = '{"thinking": "x"}'
    state = {"calls": 0}

    def next_text():
        state["calls"] += 1
        return bad if state["calls"] <= n_failures else good

    return state, next_text


async def test_responses_action_repair_retries_then_succeeds(monkeypatch):
    from pydantic import BaseModel, Field

    class Out(BaseModel):
        action: str = Field(...)

    llm = _responses_llm(monkeypatch, "low")
    state, next_text = _missing_action_response(2)
    sent_messages = []

    class FakeResponses:
        async def create(self, **kwargs):
            sent_messages.append(kwargs["input"])
            return _fake_response(output_text=next_text())

    fake_client = types.SimpleNamespace(responses=FakeResponses())
    monkeypatch.setattr(type(llm), "get_client", lambda self: fake_client)
    result = await llm.ainvoke(_messages(), output_format=Out)
    assert result.completion.action == "click"
    assert state["calls"] == 3
    assert len(sent_messages[1]) == 3 and len(sent_messages[2]) == 4


async def test_responses_action_repair_three_failures_short_error(monkeypatch):
    from pydantic import BaseModel, Field

    class Out(BaseModel):
        action: str = Field(...)

    llm = _responses_llm(monkeypatch, "low")
    _, next_text = _missing_action_response(99)

    class FakeResponses:
        async def create(self, **kwargs):
            return _fake_response(output_text=next_text())

    fake_client = types.SimpleNamespace(responses=FakeResponses())
    monkeypatch.setattr(type(llm), "get_client", lambda self: fake_client)
    with pytest.raises(ValueError) as exc:
        await llm.ainvoke(_messages(), output_format=Out)
    text = str(exc.value)
    assert "abandoned" in text and len(text) < 400


async def test_responses_streaming_pushes_reasoning_to_activity(monkeypatch):
    import app.agent.runner as runner_mod

    llm = _responses_llm(monkeypatch, "high")
    llm._activity_session = "sess-1"
    pushes: list[str] = []
    monkeypatch.setattr(
        runner_mod, "set_activity", lambda sid, label, spin=False: pushes.append(label)
    )

    final = _fake_response(reasoning_summary="thinking about the page layout")

    class FakeStream:
        def __init__(self):
            self._events = [
                types.SimpleNamespace(
                    type="response.reasoning_summary_text.delta", delta="thinking about "
                ),
                types.SimpleNamespace(
                    type="response.reasoning_summary_text.delta", delta="the page layout"
                ),
                types.SimpleNamespace(type="response.output_text.delta", delta="ok"),
            ]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def __aiter__(self):
            self._it = iter(self._events)
            return self

        async def __anext__(self):
            try:
                return next(self._it)
            except StopIteration:
                raise StopAsyncIteration

        async def get_final_response(self):
            return final

    class FakeResponses:
        def stream(self, **kwargs):
            return FakeStream()

    fake_client = types.SimpleNamespace(responses=FakeResponses())
    monkeypatch.setattr(type(llm), "get_client", lambda self: fake_client)
    result = await llm.ainvoke(_messages())
    assert result.completion == "ok"
    assert llm._last_model_reasoning == "thinking about the page layout"
    assert any(p.startswith("💭 thinking about") for p in pushes)


async def test_anthropic_drain_stream_pushes_thinking_to_activity(monkeypatch):
    import app.agent.runner as runner_mod
    from app.agent.runner import _RepairingChatAnthropic

    llm = _RepairingChatAnthropic(model="claude-sonnet-5", api_key="k")
    llm._activity_session = "sess-2"
    pushes: list[str] = []
    monkeypatch.setattr(
        runner_mod, "set_activity", lambda sid, label, spin=False: pushes.append(label)
    )

    events = [
        types.SimpleNamespace(
            type="content_block_delta",
            delta=types.SimpleNamespace(type="thinking_delta", thinking="checking the "),
        ),
        types.SimpleNamespace(
            type="content_block_delta",
            delta=types.SimpleNamespace(type="thinking_delta", thinking="listing panel"),
        ),
    ]

    class FakeStream:
        def __aiter__(self):
            self._it = iter(events)
            return self

        async def __anext__(self):
            try:
                return next(self._it)
            except StopIteration:
                raise StopAsyncIteration

        async def get_final_message(self):
            return "final"

    assert await llm._drain_stream(FakeStream()) == "final"
    assert any(p.startswith("💭 checking the") for p in pushes)


def _review_history(done: bool, verdict, reason: str = "needs work", steps: int = 1):
    import types as _t

    judgement = None
    if verdict is not None:
        judgement = _t.SimpleNamespace(
            verdict=verdict, failure_reason=reason, reasoning=""
        )
    step = _t.SimpleNamespace(result=[_t.SimpleNamespace(judgement=judgement)])
    return _t.SimpleNamespace(
        history=[step] * steps,
        is_done=lambda: done,
        is_successful=lambda: done,
    )


class _ReviewStore:
    def __init__(self) -> None:
        self.value = "v1"

    def read_output(self) -> str:
        return self.value


async def test_run_with_review_loops_until_reviewer_passes(monkeypatch) -> None:
    import types as _t

    from app.agent import runner as runner_mod

    events: list[str] = []

    async def fake_create_message(**kwargs):
        events.append(kwargs.get("summary") or "")

    monkeypatch.setattr(runner_mod.crud, "create_message", fake_create_message)

    histories = [
        _review_history(True, False, "fix the empty fields", steps=1),
        _review_history(True, True, steps=2),
    ]
    tasks: list[str] = []
    agent = _t.SimpleNamespace(add_new_task=lambda msg: tasks.append(msg))
    store = _ReviewStore()

    async def run_agent():
        return histories.pop(0)

    final = await runner_mod._run_with_review(agent, store, "sid", run_agent)
    assert final.is_successful() is True
    assert len(tasks) == 1
    assert "fix the empty fields" in tasks[0]
    assert "you may reply via done" in tasks[0]
    assert events == ["fix the empty fields"]


async def test_run_with_review_forces_changes_after_two_justifications(
    monkeypatch,
) -> None:
    import types as _t

    from app.agent import runner as runner_mod

    async def fake_create_message(**kwargs):
        return None

    monkeypatch.setattr(runner_mod.crud, "create_message", fake_create_message)

    histories = [
        _review_history(True, False, "round 1", steps=1),
        _review_history(True, False, "round 2", steps=2),
        _review_history(True, False, "round 3", steps=3),
        _review_history(True, False, "round 4", steps=4),
    ]
    tasks: list[str] = []
    agent = _t.SimpleNamespace(add_new_task=lambda msg: tasks.append(msg))
    store = _ReviewStore()

    async def run_agent():
        return histories.pop(0)

    await runner_mod._run_with_review(agent, store, "sid", run_agent)
    assert len(tasks) == runner_mod._MAX_REVIEW_ROUNDS
    assert "you may reply via done" in tasks[0]
    assert "you may reply via done" in tasks[1]
    assert "your replies are used up" in tasks[2]


async def test_run_with_review_skips_failed_or_passing_runs(monkeypatch) -> None:
    import types as _t

    from app.agent import runner as runner_mod

    called: list[str] = []

    async def fake_create_message(**kwargs):
        called.append("event")

    monkeypatch.setattr(runner_mod.crud, "create_message", fake_create_message)
    agent = _t.SimpleNamespace(add_new_task=lambda msg: called.append("task"))

    async def run_pass():
        return _review_history(True, True)

    await runner_mod._run_with_review(agent, None, "sid", run_pass)

    async def run_failed():
        return _review_history(False, False)

    await runner_mod._run_with_review(agent, None, "sid", run_failed)
    assert called == []


async def test_run_with_review_stops_when_round_adds_no_steps(monkeypatch) -> None:
    import types as _t

    from app.agent import runner as runner_mod

    events: list[str] = []

    async def fake_create_message(**kwargs):
        events.append(kwargs.get("summary") or "")

    monkeypatch.setattr(runner_mod.crud, "create_message", fake_create_message)

    def dead_history():
        return _review_history(True, False, "still wrong")

    histories = [dead_history(), dead_history(), dead_history(), dead_history()]
    tasks: list[str] = []
    agent = _t.SimpleNamespace(add_new_task=lambda msg: tasks.append(msg))

    async def run_agent():
        return histories.pop(0)

    await runner_mod._run_with_review(agent, _ReviewStore(), "sid", run_agent)
    assert len(tasks) == 1
    assert any("no agent activity" in e for e in events)


def test_browser_sessions_keep_alive_for_review_rounds() -> None:
    import inspect

    from app.agent import runner as runner_mod

    src = inspect.getsource(runner_mod.run_agent_session)
    assert "browser_session.browser_profile.keep_alive = True" in src


async def test_invoke_repair_names_mistyped_arguments():
    from app.agent.runner import _invoke_with_action_repair

    calls = []

    async def fake_invoke(messages):
        calls.append(messages)
        if len(calls) == 1:
            raise ValueError(
                "1 validation error for AgentOutput\n"
                "action.0.read_pages.urls\n"
                "  Input should be a valid list [type=list_type]"
            )
        return "ok"

    result = await _invoke_with_action_repair(fake_invoke, [], object)
    assert result == "ok"
    correction = calls[1][-1].content
    assert "action.0.read_pages.urls" in correction
    assert "no executable" not in correction


def test_friendly_error_clips_to_first_sentence():
    from app.agent.runner import _friendly_error

    long = (
        "'[\"companyName\", \"companyUrl\"]' is not a schema field. Fields: "
        + ", ".join(f"field{i}" for i in range(40))
    )
    short = _friendly_error(long)
    assert short.endswith("…")
    assert len(short) < 160
    assert "is not a schema field" in short
    assert _friendly_error("tiny error") == "tiny error"
