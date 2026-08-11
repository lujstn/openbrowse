"""Tests for the JSON-Schema -> Pydantic converter (app/agent/schema.py)."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.agent.schema import SchemaConversionError, json_schema_to_pydantic

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def test_optional_enum_and_forbid_extra():
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["found"],
        "properties": {
            "found": {"type": "boolean"},
            "reason": {
                "anyOf": [
                    {"type": "string", "enum": ["not_found", "auth_wall"]},
                    {"type": "null"},
                ]
            },
            "title": {"anyOf": [{"type": "string", "description": "the title"}, {"type": "null"}]},
        },
    }
    model = json_schema_to_pydantic(schema, "Syn")
    assert model.model_validate({"found": True}).reason is None
    assert model.model_validate({"found": True, "reason": "auth_wall"}).reason == "auth_wall"
    assert model.model_fields["title"].description == "the title"
    with pytest.raises(ValidationError):
        model.model_validate({"found": True, "reason": "bogus"})
    with pytest.raises(ValidationError):
        model.model_validate({"found": True, "extra": 1})


def test_loose_object_allows_extra():
    schema = {
        "type": "object",
        "additionalProperties": {},
        "properties": {"a": {"type": "string"}},
    }
    model = json_schema_to_pydantic(schema)
    assert model.model_validate({"a": "x", "surprise": 1}).a == "x"


def test_true_union_raises():
    with pytest.raises(SchemaConversionError):
        json_schema_to_pydantic(
            {"type": "object", "properties": {"x": {"anyOf": [{"type": "string"}, {"type": "integer"}]}}}
        )


def test_non_identifier_property_raises():
    with pytest.raises(SchemaConversionError):
        json_schema_to_pydantic({"type": "object", "properties": {"a-b": {"type": "string"}}})


def test_cloud_schema_1_builds_and_validates():
    model = json_schema_to_pydantic(_load("cloud_output_schema_1.json"), "CloudOne")
    obj = model.model_validate({"found": True, "socialLinks": []})
    assert obj.found is True
    assert obj.title is None


def test_cloud_schema_2_builds_and_enforces_enum():
    model = json_schema_to_pydantic(_load("cloud_output_schema_2.json"), "CloudTwo")
    ok = model.model_validate(
        {"jobs": [{"title": "Engineer", "locationType": "HYBRID"}], "careersPageUrl": "https://x"}
    )
    assert ok.jobs[0].title == "Engineer"
    with pytest.raises(ValidationError):
        model.model_validate({"jobs": [{"locationType": "NOT_A_REAL_ENUM"}]})
