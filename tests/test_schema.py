"""Tests for the JSON-Schema -> Pydantic converter (app/agent/schema.py)."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from openbrowse.agent.schema import (
    SchemaConversionError,
    json_schema_to_pydantic,
    schema_directives,
)

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
            "applyUrl": "https://apply.example.com/m/x/application",
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
    url = "https://www.example.com/list?embed_jid=034a8a61#openings"
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


def test_email_fields_validated_by_format_and_name():
    schema = {
        "type": "object",
        "properties": {
            "contact": {"type": "string", "format": "email"},
            "recruiterEmail": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "emailBody": {"type": "string"},
        },
    }
    model = json_schema_to_pydantic(schema, "Em")
    ok = model.model_validate(
        {
            "contact": "hello@example.com",
            "recruiterEmail": "a.b+tag@sub.example.co.uk",
            "emailBody": "Dear hiring manager, ...",
        }
    )
    assert ok.contact == "hello@example.com"
    assert ok.emailBody.startswith("Dear")
    assert model.model_validate({"contact": "x@y.z"}).recruiterEmail is None
    for bad in ("Powered by", "hello at example", "a@b", "@y.z", "a@"):
        with pytest.raises(ValidationError, match="not an email"):
            model.model_validate({"contact": bad})


def test_uuid_fields_validated_by_format_and_name():
    schema = {
        "type": "object",
        "properties": {
            "token": {"type": "string", "format": "uuid"},
            "companyUuid": {"type": "string"},
        },
    }
    model = json_schema_to_pydantic(schema, "Uu")
    u = "3dee50f9-717b-4311-b1f3-1da2cef18c20"
    ok = model.model_validate({"token": u, "companyUuid": u.upper()})
    assert ok.token == u
    with pytest.raises(ValidationError, match="not a UUID"):
        model.model_validate({"token": "not-a-uuid", "companyUuid": u})


def test_id_fields_reject_prose_but_stay_broad():
    schema = {
        "type": "object",
        "properties": {
            "companySourceId": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "id": {"type": "string"},
            "idea": {"type": "string"},
        },
    }
    model = json_schema_to_pydantic(schema, "Ids")
    ok = model.model_validate(
        {"companySourceId": "acme-42_x", "id": "12345", "idea": "free text here"}
    )
    assert ok.companySourceId == "acme-42_x"
    assert ok.idea == "free text here"
    for bad in ("Powered by", "two words", "", " ", "x" * 129):
        with pytest.raises(ValidationError, match="not an identifier"):
            model.model_validate({"companySourceId": bad, "id": "1"})


def test_explicit_format_opts_out_of_all_name_guards():
    schema = {
        "type": "object",
        "properties": {
            "recordId": {"type": "string", "format": "date-time"},
            "shareLink": {"type": "string", "format": "date-time"},
        },
    }
    model = json_schema_to_pydantic(schema, "OptOut2")
    ok = model.model_validate({"recordId": "yesterday at noon", "shareLink": "n/a"})
    assert ok.recordId == "yesterday at noon"


def test_uuid_suffix_wins_over_id_suffix():
    schema = {"type": "object", "properties": {"companyUuid": {"type": "string"}}}
    model = json_schema_to_pydantic(schema, "Prec")
    with pytest.raises(ValidationError, match="not a UUID"):
        model.model_validate({"companyUuid": "plain-token"})


ORG_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["found", "socialLinks"],
    "properties": {
        "found": {"type": "boolean"},
        "reason": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "title": {
            "anyOf": [{"type": "string"}, {"type": "null"}],
            "default": "Arize AI | AI Observability",
        },
        "socialLinks": {"type": "array", "items": {"type": "string"}},
    },
    "if": {"properties": {"found": {"const": False}}},
    "then": {"required": ["reason"]},
    "else": {"not": {"required": ["reason"]}},
}


def test_schema_directives_reads_defaults_and_conditionals():
    d = schema_directives(ORG_SCHEMA)
    assert d.defaults == {"title": "Arize AI | AI Observability"}
    assert d.excused_fields({"found": True}) == frozenset({"reason"})
    assert d.excused_fields({"found": False}) == frozenset()


def test_schema_directives_still_converts_to_a_model():
    """The keywords must not disturb the shape the model builder sees."""
    model = json_schema_to_pydantic(ORG_SCHEMA, "Org")
    assert set(model.model_fields) == {"found", "reason", "title", "socialLinks"}


@pytest.mark.parametrize(
    "schema",
    [
        None,
        "not a dict",
        {},
        {"if": "nonsense", "then": 42},
        {"if": {"properties": {"found": {"minimum": 1}}}, "then": {"required": ["r"]}},
        {"if": {"properties": {}}, "else": {"not": {"required": ["r"]}}},
        {"then": {"required": ["r"]}},
    ],
)
def test_schema_directives_ignores_what_it_cannot_read(schema):
    """Anything richer than the narrow form leaves today's behaviour untouched."""
    d = schema_directives(schema)
    assert d.conditionals == ()
    assert d.excused_fields({"found": True}) == frozenset()


def test_schema_directives_keeps_asking_when_a_rule_contradicts_itself():
    d = schema_directives(
        {
            "type": "object",
            "properties": {"found": {"type": "boolean"}},
            "if": {"properties": {"found": {"const": True}}},
            "then": {"required": ["reason"], "not": {"required": ["reason"]}},
        }
    )
    assert d.excused_fields({"found": True}) == frozenset()


def test_schema_directives_handles_a_multi_property_condition():
    d = schema_directives(
        {
            "if": {"properties": {"found": {"const": True}, "kind": {"const": "org"}}},
            "then": {"not": {"required": ["reason"]}},
        }
    )
    assert d.excused_fields({"found": True, "kind": "org"}) == frozenset({"reason"})
    assert d.excused_fields({"found": True, "kind": "event"}) == frozenset()
