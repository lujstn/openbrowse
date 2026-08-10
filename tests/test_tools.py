"""Tool registration tests (no live API calls)."""

from unittest.mock import patch

from browser_use import Tools


def test_register_fetch_tool() -> None:
    tools = Tools()
    from app.agent.tools import register_fetch_tool

    register_fetch_tool(tools)
    assert "http_fetch" in tools.registry.registry.actions


def test_register_capsolver_with_key() -> None:
    with patch("app.agent.tools.settings") as mock_settings:
        mock_settings.capsolver_api_key = "test-key"
        tools = Tools()
        from app.agent.tools import register_capsolver_tool

        register_capsolver_tool(tools)
        assert "solve_captcha" in tools.registry.registry.actions


def test_register_capsolver_without_key() -> None:
    with patch("app.agent.tools.settings") as mock_settings:
        mock_settings.capsolver_api_key = ""
        tools = Tools()
        from app.agent.tools import register_capsolver_tool

        register_capsolver_tool(tools)
        assert "solve_captcha" not in tools.registry.registry.actions


def test_register_python_sandbox() -> None:
    tools = Tools()
    from app.agent.tools import register_python_sandbox_tool

    register_python_sandbox_tool(tools)
    assert "run_python" in tools.registry.registry.actions


def test_parse_capsolver_cost() -> None:
    from app.agent.tools import _parse_capsolver_cost

    assert _parse_capsolver_cost({"cost": "0.0008"}) == 0.0008
    assert _parse_capsolver_cost({"cost": 0.0012}) == 0.0012
    assert _parse_capsolver_cost({}) == 0.0
    assert _parse_capsolver_cost({"cost": None}) == 0.0
    assert _parse_capsolver_cost({"cost": "not-a-number"}) == 0.0
