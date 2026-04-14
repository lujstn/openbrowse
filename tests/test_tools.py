"""Tool registration tests (no live API calls)."""

from unittest.mock import patch

from browser_use import Controller


def test_register_fetch_tool() -> None:
    controller = Controller()
    from app.agent.tools import register_fetch_tool

    register_fetch_tool(controller)
    assert "http_fetch" in controller.registry.registry.actions


def test_register_capsolver_with_key() -> None:
    with patch("app.agent.tools.settings") as mock_settings:
        mock_settings.capsolver_api_key = "test-key"
        controller = Controller()
        from app.agent.tools import register_capsolver_tool

        register_capsolver_tool(controller)
        assert "solve_captcha" in controller.registry.registry.actions


def test_register_capsolver_without_key() -> None:
    with patch("app.agent.tools.settings") as mock_settings:
        mock_settings.capsolver_api_key = ""
        controller = Controller()
        from app.agent.tools import register_capsolver_tool

        register_capsolver_tool(controller)
        assert "solve_captcha" not in controller.registry.registry.actions


def test_register_python_sandbox() -> None:
    controller = Controller()
    from app.agent.tools import register_python_sandbox_tool

    register_python_sandbox_tool(controller)
    assert "run_python" in controller.registry.registry.actions
