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


def test_register_code_tools() -> None:
    tools = Tools()
    from app.agent.tools import register_code_tools

    register_code_tools(tools)
    actions = tools.registry.registry.actions
    assert "write_code_file" in actions
    assert "run_code_file" in actions
    assert "run_python" not in actions


def test_normalise_py_name() -> None:
    from app.agent.tools import _normalise_py_name

    assert _normalise_py_name("extract") == "extract.py"
    assert _normalise_py_name("extract.py") == "extract.py"
    assert _normalise_py_name("a/b/scrape") == "scrape.py"
    assert _normalise_py_name("weird name!.txt") == "weird_name_.txt.py"
    assert _normalise_py_name("") == "script.py"


def test_parse_capsolver_cost() -> None:
    from app.agent.tools import _parse_capsolver_cost

    assert _parse_capsolver_cost({"cost": "0.0008"}) == 0.0008
    assert _parse_capsolver_cost({"cost": 0.0012}) == 0.0012
    assert _parse_capsolver_cost({}) == 0.0
    assert _parse_capsolver_cost({"cost": None}) == 0.0
    assert _parse_capsolver_cost({"cost": "not-a-number"}) == 0.0


def test_item_url_field_prefers_detail_over_company() -> None:
    from app.agent.output_store import OutputStore
    from app.agent.schema import json_schema_to_pydantic
    from app.agent.tools import _item_url_field

    schema = {
        "type": "object",
        "properties": {
            "jobs": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "companyUrl": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                        "sourceUrl": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                        "applyUrl": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                    },
                },
            }
        },
    }
    store = OutputStore(json_schema_to_pydantic(schema, "T"))
    assert _item_url_field(store) == "sourceUrl"


def test_item_url_field_falls_back_to_bare_url() -> None:
    from app.agent.output_store import OutputStore
    from app.agent.schema import json_schema_to_pydantic
    from app.agent.tools import _item_url_field

    schema = {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "companyUrl": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                        "url": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                    },
                },
            }
        },
    }
    store = OutputStore(json_schema_to_pydantic(schema, "T"))
    assert _item_url_field(store) == "url"
