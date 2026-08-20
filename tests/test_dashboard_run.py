"""Unit tests for dashboard run helpers — port formula and model option list."""

from openbrowse.dashboard.routes import (
    MODEL_OPTIONS,
    reasoning_options_map,
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
        "gpt-5.6-luna",
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
    ]
    from openbrowse.agent.runner import _resolve_model

    for value in values:
        assert _resolve_model(value)[1]


def test_reasoning_options_map_covers_all_models_with_defaults():
    options_map = reasoning_options_map()
    assert set(options_map) == {v for v, _ in MODEL_OPTIONS}
    for value, spec in options_map.items():
        values = [v for v, _ in spec["options"]]
        assert spec["default"] in values, value
        labels = dict(spec["options"])
        assert "Default" in labels[spec["default"]] or "Recommended" in labels[spec["default"]]


def test_reasoning_options_per_generation():
    """"Default" has to name the level the session actually runs at. Where our
    pick differs from the provider's, the provider's is labelled as such rather
    than as the default, which it no longer is."""
    options_map = reasoning_options_map()
    sonnet5 = options_map["claude-sonnet-5"]
    assert sonnet5["default"] == "high"
    assert dict(sonnet5["options"])["high"] == "High (Default)"
    assert dict(sonnet5["options"])["none"] == "None"
    assert "off" not in dict(sonnet5["options"])
    opus48 = options_map["claude-opus-4-8[1m]"]
    assert opus48["default"] == "none"
    assert dict(opus48["options"])["none"] == "None (Default)"
    assert "xhigh" in dict(opus48["options"])
    fable = options_map["claude-fable-5"]
    assert "none" not in dict(fable["options"])
    assert fable["default"] == "high"
    terra = options_map["gpt-5.6-terra"]
    assert terra["default"] == "none"
    assert dict(terra["options"])["none"] == "None (Default)"
    assert dict(terra["options"])["medium"] == "Medium (Provider default)"
    assert dict(terra["options"])["max"] == "Max"
    luna = options_map["gpt-5.6-luna"]
    assert luna["default"] == "max"
    assert dict(luna["options"])["max"] == "Max (Default)"
    assert dict(luna["options"])["medium"] == "Medium (Provider default)"
    sonnet46 = options_map["claude-sonnet-4-6"]
    assert "xhigh" not in dict(sonnet46["options"])
    assert sonnet46["default"] == "none"


def test_no_option_is_labelled_default_unless_it_is_the_one_used():
    """The preselected value and the one marked Default must be the same option,
    or the dropdown contradicts itself."""
    from openbrowse.agent.runner import effort_when_unset

    for model, spec in reasoning_options_map().items():
        labelled = [v for v, label in spec["options"] if "(Default)" in label]
        assert labelled == [spec["default"]], model
        assert spec["default"] == effort_when_unset(model), model


def test_model_provider_labels():
    assert model_provider("gpt-5.6-luna") == "OpenAI"
    assert model_provider("claude-opus-4-8[1m]") == "Anthropic"
    assert model_provider("claude-sonnet-5") == "Anthropic"
    assert model_provider(None) == "Anthropic"


def test_live_sessions_filters_running_with_url_and_caps(monkeypatch):
    from dataclasses import replace

    from openbrowse.config import settings
    from openbrowse.dashboard import routes as routes_mod

    monkeypatch.setattr(
        routes_mod, "settings", replace(settings, max_concurrent_sessions=5)
    )
    sessions = [
        {"id": "a", "status": "running", "live_url": "/vnc/a/vnc.html"},
        {"id": "b", "status": "running", "live_url": None},
        {"id": "c", "status": "created", "live_url": None},
        {"id": "d", "status": "stopped", "live_url": "/vnc/d/vnc.html"},
        {"id": "e", "status": "running", "live_url": "/vnc/e/vnc.html"},
    ]
    live = _live_sessions(sessions)
    assert [s["id"] for s in live] == ["a", "e"]


def test_message_display_includes_model_reasoning_card():
    import json

    from openbrowse.dashboard.routes import message_display

    row = {
        "type": "result",
        "summary": "click 12",
        "data": json.dumps(
            {"action": "click", "thinking": "step reasoning", "model_reasoning": "native reasoning"}
        ),
    }
    md = message_display(row)
    assert md["thinking"] == "step reasoning"
    assert md["model_reasoning"] == "native reasoning"


def test_strip_thinking_removes_reasoning_keys_including_legacy():
    import json

    from openbrowse.dashboard.routes import _strip_thinking

    data = json.dumps(
        {"see": "a", "thinking": "b", "model_reasoning": "c", "model_thinking": "d"}
    )
    stripped = json.loads(_strip_thinking(data))
    assert "thinking" not in stripped
    assert "model_reasoning" not in stripped
    assert "model_thinking" not in stripped
    assert stripped["see"] == "a"


def test_mdlite_escapes_then_formats():
    from openbrowse.dashboard.routes import _mdlite

    out = str(_mdlite("**Plan** use `run_code_file`\n<script>x</script>"))
    assert "<strong>Plan</strong>" in out
    assert "<code>run_code_file</code>" in out
    assert "<br>" in out
    assert "<script>" not in out and "&lt;script&gt;" in out


def test_message_display_reasoning_event_row():
    import json

    from openbrowse.dashboard.routes import message_display

    row = {
        "type": "event",
        "summary": "short preview",
        "data": json.dumps(
            {"category": "reasoning", "action": "model_reasoning", "reasoning": "**Full** text"}
        ),
    }
    md = message_display(row)
    assert md["category"] == "reasoning"
    assert md["reasoning"] == "**Full** text"


def test_message_display_error_full_card():
    import json as _json

    from openbrowse.dashboard.routes import message_display

    row = {
        "type": "browser_action_error",
        "summary": "Error: '[\"companyName\", ...]' is not a schema field",
        "data": _json.dumps(
            {
                "step": 18,
                "category": "schema",
                "action": "mark_absent",
                "error_full": "'[\"companyName\", \"companyUrl\"]' is not a schema field. Fields: title, description",
                "thinking": "settling absent fields",
            }
        ),
    }
    md = message_display(row)
    assert md["category"] == "error"
    assert "Fields: title, description" in md["error_full"]
    assert md["thinking"] == "settling absent fields"


def test_usd_filter_rounds_up_to_cent():
    from openbrowse.dashboard.routes import _usd

    assert _usd(0.399) == "0.40"
    assert _usd(0.2358) == "0.24"
    assert _usd(0.401) == "0.41"
    assert _usd(0.40) == "0.40"
    assert _usd(0) == "0.00"
    assert _usd(None) == "0.00"


def test_message_display_result_snippet_card():
    import json as _json

    from openbrowse.dashboard.routes import message_display

    row = {
        "type": "result",
        "summary": "Read every page from the saved link set",
        "data": _json.dumps(
            {
                "step": 3,
                "category": "read",
                "action": "read_pages",
                "result_snippet": "Read 14 of 14 pages; full content saved to 'pages.json'.",
            }
        ),
    }
    md = message_display(row)
    assert md["result_snippet"].startswith("Read 14 of 14 pages")


def test_recommendation_table_covers_only_real_models():
    """A typo here silently downgrades a model to the provider default rather
    than failing, so every key must resolve."""
    from openbrowse.agent.runner import _RECOMMENDED_EFFORT, _resolve_model, valid_efforts

    for model, effort in _RECOMMENDED_EFFORT.items():
        assert _resolve_model(model)[1]
        assert effort in valid_efforts(model), f"{model} cannot run at {effort}"
