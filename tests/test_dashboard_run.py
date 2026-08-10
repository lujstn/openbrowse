"""Unit tests for dashboard run helpers — port formula and model option list."""

from app.dashboard.routes import (
    MODEL_OPTIONS,
    THINKING_OPTIONS,
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
        "claude-opus-4-8",
        "claude-opus-4-8[1m]",
        "claude-opus-5",
        "gpt-5.6-luna",
        "gpt-5.6-terra",
        "gpt-5.6-sol",
    ]


def test_thinking_options_off_default_first():
    assert THINKING_OPTIONS[0][0] == "off"
    assert [v for v, _ in THINKING_OPTIONS] == ["off", "low", "medium", "high"]


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
