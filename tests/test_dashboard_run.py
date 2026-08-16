"""Unit tests for dashboard run helpers — port formula and model option list."""

from app.dashboard.routes import (
    MODEL_OPTIONS,
    thinking_options_map,
    _live_sessions,
    _novnc_port_for_display,
    model_provider,
)


def test_novnc_port_formula_matches_allocation():
    assert _novnc_port_for_display(10) == 6080
    assert _novnc_port_for_display(11) == 6081
    assert _novnc_port_for_display(14) == 6084


def test_model_options_curated_list():
    values = [value for value, _ in MODEL_OPTIONS]
    assert values[0] == "claude-sonnet-5"
    assert values == [
        "claude-sonnet-5",
        "gpt-5.6-terra",
        "gpt-5.6-sol",
        "claude-opus-5",
        "claude-fable-5",
        "claude-mythos-5",
        "claude-opus-4-8",
        "claude-opus-4-8[1m]",
        "claude-opus-4-7",
        "claude-opus-4-7[1m]",
        "claude-opus-4-6",
        "claude-opus-4-6[1m]",
        "claude-sonnet-4-6",
        "claude-sonnet-4-6[1m]",
        "gpt-5.6-luna",
    ]
    from app.agent.runner import _resolve_model

    for value in values:
        assert _resolve_model(value)[1]


def test_thinking_options_map_covers_all_models_with_defaults():
    options_map = thinking_options_map()
    assert set(options_map) == {v for v, _ in MODEL_OPTIONS}
    for value, spec in options_map.items():
        values = [v for v, _ in spec["options"]]
        assert spec["default"] in values, value
        labels = dict(spec["options"])
        assert "(Default)" in labels[spec["default"]] or labels[spec["default"]] == "Model Default"


def test_thinking_options_per_generation():
    options_map = thinking_options_map()
    sonnet5 = options_map["claude-sonnet-5"]
    assert sonnet5["default"] == "high"
    assert dict(sonnet5["options"])["high"] == "High (Default)"
    assert dict(sonnet5["options"])["off"] == "Off"
    opus48 = options_map["claude-opus-4-8[1m]"]
    assert opus48["default"] == "off"
    assert dict(opus48["options"])["off"] == "None (Default)"
    assert "xhigh" in dict(opus48["options"])
    fable = options_map["claude-fable-5"]
    assert "off" not in dict(fable["options"])
    assert fable["default"] == "high"
    terra = options_map["gpt-5.6-terra"]
    assert terra["default"] == "default"
    assert dict(terra["options"])["default"] == "Model Default"
    assert "max" not in dict(terra["options"])
    sonnet46 = options_map["claude-sonnet-4-6"]
    assert "xhigh" not in dict(sonnet46["options"])


def test_model_provider_labels():
    assert model_provider("gpt-5.6-luna") == "OpenAI"
    assert model_provider("claude-opus-4-8[1m]") == "Anthropic"
    assert model_provider("claude-sonnet-5") == "Anthropic"
    assert model_provider(None) == "Anthropic"


def test_live_sessions_filters_running_with_url_and_caps():
    sessions = [
        {"id": "a", "status": "running", "live_url": "/vnc/a/vnc.html"},
        {"id": "b", "status": "running", "live_url": None},
        {"id": "c", "status": "created", "live_url": None},
        {"id": "d", "status": "stopped", "live_url": "/vnc/d/vnc.html"},
        {"id": "e", "status": "running", "live_url": "/vnc/e/vnc.html"},
    ]
    live = _live_sessions(sessions)
    assert [s["id"] for s in live] == ["a", "e"]


def test_message_display_includes_model_thinking_card():
    import json

    from app.dashboard.routes import message_display

    row = {
        "type": "result",
        "summary": "click 12",
        "data": json.dumps(
            {"action": "click", "thinking": "step reasoning", "model_thinking": "native reasoning"}
        ),
    }
    md = message_display(row)
    assert md["thinking"] == "step reasoning"
    assert md["model_thinking"] == "native reasoning"


def test_strip_thinking_removes_both_thinking_keys():
    import json

    from app.dashboard.routes import _strip_thinking

    data = json.dumps({"see": "a", "thinking": "b", "model_thinking": "c"})
    stripped = json.loads(_strip_thinking(data))
    assert "thinking" not in stripped
    assert "model_thinking" not in stripped
    assert stripped["see"] == "a"
