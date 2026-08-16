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
        {"items": [{"title": "Widget", "condition": "USED"}], "indexPageUrl": "https://x"}
    )
    assert ok.items[0].title == "Widget"
    with pytest.raises(ValidationError):
        model.model_validate({"items": [{"condition": "NOT_A_REAL_ENUM"}]})


def test_url_fields_validated_by_format():
    schema = {
        "type": "object",
        "properties": {
            "homepage": {"type": "string", "format": "uri"},
            "note": {"type": "string"},
        },
    }
    model = json_schema_to_pydantic(schema, "Fmt")
    ok = model.model_validate({"homepage": "https://example.com/x", "note": "Powered by"})
    assert ok.homepage == "https://example.com/x"
    for bad in ("Powered by", "example.com/x", "/jobs/3", "ftp://example.com", ""):
        with pytest.raises(ValidationError, match="absolute http"):
            model.model_validate({"homepage": bad})


def test_url_fields_validated_by_name_suffix():
    schema = {
        "type": "object",
        "properties": {
            "applyUrl": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "sourceURI": {"type": "string"},
            "href": {"type": "string"},
            "hyperLink": {"type": "string"},
            "curl": {"type": "string"},
            "linkText": {"type": "string"},
        },
    }
    model = json_schema_to_pydantic(schema, "Named")
    good = model.model_validate(
        {
            "applyUrl": "https://jobs.ashbyhq.com/m/x/application",
            "sourceURI": "http://a.b/c",
            "href": "https://a.b",
            "hyperLink": "https://a.b",
            "curl": "not a url",
            "linkText": "Apply here",
        }
    )
    assert good.curl == "not a url"
    assert good.linkText == "Apply here"
    assert model.model_validate({}).applyUrl is None
    for field in ("applyUrl", "sourceURI", "href", "hyperLink"):
        with pytest.raises(ValidationError, match="absolute http"):
            model.model_validate({field: "Powered by"})


def test_url_validation_trims_and_preserves_value():
    schema = {"type": "object", "properties": {"sourceUrl": {"type": "string"}}}
    model = json_schema_to_pydantic(schema, "Trim")
    url = "https://www.marshmallow.com/jobs?ashby_jid=034a8a61#openings"
    assert model.model_validate({"sourceUrl": f"  {url} "}).sourceUrl == url


def test_explicit_non_url_format_opts_out_of_name_heuristic():
    schema = {
        "type": "object",
        "properties": {"shareLink": {"type": "string", "format": "date-time"}},
    }
    model = json_schema_to_pydantic(schema, "OptOut")
    assert model.model_validate({"shareLink": "not a url"}).shareLink == "not a url"


def test_url_format_inside_array_items():
    schema = {
        "type": "object",
        "properties": {
            "recordingUrls": {
                "type": "array",
                "items": {"type": "string", "format": "uri"},
            }
        },
    }
    model = json_schema_to_pydantic(schema, "Arr")
    assert model.model_validate({"recordingUrls": ["https://a.b/x"]}).recordingUrls
    with pytest.raises(ValidationError, match="absolute http"):
        model.model_validate({"recordingUrls": ["https://a.b/x", "nope"]})
