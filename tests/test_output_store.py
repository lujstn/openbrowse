"""Tests for the schema-validated output store (app/agent/output_store.py).

Pure — no browser-use import — so they run in the repo's local venv.
"""

import json

import pytest

from openbrowse.agent.output_store import OutputStore
from openbrowse.agent.schema import json_schema_to_pydantic, schema_directives

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["items", "indexPageUrl"],
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["title", "url"],
                "properties": {
                    "title": {"type": "string"},
                    "department": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                    "location": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                    "url": {"type": "string"},
                    "description": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                    "publishedAt": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                },
            },
        },
        "indexPageUrl": {"type": "string"},
    },
}


def _store() -> OutputStore:
    return OutputStore(json_schema_to_pydantic(SCHEMA, "TaskOutput"))


def test_empty_from_schema():
    s = _store()
    assert s.data == {"items": [], "indexPageUrl": None}
    assert s.array_field == "items"
    assert s.item_model is not None
    assert s.is_empty() is True
    assert json.loads(s.read_output()) == {"items": [], "indexPageUrl": None}


def test_add_item_valid_fills_optional_fields_as_null():
    s = _store()
    ok, msg = s.add_item({"title": "Engineer", "url": "https://x/1"})
    assert ok is True
    assert "#0" in msg
    job = s.data["items"][0]
    assert job == {
        "title": "Engineer",
        "department": None,
        "location": None,
        "url": "https://x/1",
        "description": None,
        "publishedAt": None,
    }
    assert s.is_empty() is False


def test_add_item_missing_required_rejected():
    s = _store()
    ok, msg = s.add_item({"title": "No URL"})
    assert ok is False
    assert "url" in msg
    assert s.data["items"] == []


def test_add_item_extra_field_rejected():
    s = _store()
    ok, msg = s.add_item({"title": "X", "url": "https://x.com/u", "salary": "lots"})
    assert ok is False
    assert s.data["items"] == []


def test_add_item_wrong_type_rejected():
    s = _store()
    ok, msg = s.add_item({"title": 123, "url": "https://x.com/u"})
    assert ok is False


def test_update_item_enriches_stub():
    s = _store()
    s.add_item({"title": "Engineer", "url": "https://x/1"})
    ok, msg = s.update_item(0, {"description": "Build things", "publishedAt": "2026-01-02"})
    assert ok is True
    assert s.data["items"][0]["description"] == "Build things"
    assert s.data["items"][0]["publishedAt"] == "2026-01-02"
    assert s.data["items"][0]["title"] == "Engineer"


def test_update_item_bad_index_rejected():
    s = _store()
    s.add_item({"title": "Engineer", "url": "https://x/1"})
    ok, msg = s.update_item(5, {"description": "x"})
    assert ok is False
    assert "No item at index 5" in msg


def test_update_item_revalidates_merge():
    s = _store()
    s.add_item({"title": "Engineer", "url": "https://x/1"})
    ok, msg = s.update_item(0, {"title": 999})
    assert ok is False
    assert s.data["items"][0]["title"] == "Engineer"


def test_set_field_valid():
    s = _store()
    ok, msg = s.set_field("indexPageUrl", "https://co/about")
    assert ok is True
    assert s.data["indexPageUrl"] == "https://co/about"


def test_set_field_wrong_type_rejected():
    s = _store()
    ok, msg = s.set_field("indexPageUrl", {"not": "a string"})
    assert ok is False
    assert s.data["indexPageUrl"] is None


def test_set_field_unknown_key_rejected():
    s = _store()
    ok, msg = s.set_field("nope", "x")
    assert ok is False
    assert "not an output field" in msg


def test_set_field_on_array_rejected():
    s = _store()
    ok, msg = s.set_field("items", [])
    assert ok is False
    assert "add_item" in msg


def test_search_output():
    s = _store()
    s.add_item({"title": "Backend Engineer", "url": "https://x/1"})
    s.add_item({"title": "Designer", "url": "https://x/2"})
    s.set_field("indexPageUrl", "https://co/about")
    res = json.loads(s.search_output("engineer"))
    assert "items" in res
    assert len(res["items"]) == 1
    assert res["items"][0]["index"] == 0
    res2 = json.loads(s.search_output("about"))
    assert res2.get("indexPageUrl") == "https://co/about"
    assert json.loads(s.search_output("zzz-not-present")) == {}


def test_empty_fields_flags_missing_details():
    s = _store()
    s.add_item({"title": "A", "url": "https://x.com/1"})
    s.add_item({"title": "B", "url": "https://x.com/2", "description": "has one"})
    flags = s.empty_fields()
    joined = " | ".join(flags)
    assert "indexPageUrl (not set)" in flags
    assert "description — empty on 1 of 2 items" in joined
    assert "publishedAt — empty on 2 of 2 items" in joined


def test_empty_fields_empty_list():
    s = _store()
    flags = s.empty_fields()
    assert any("items (list is empty)" in f for f in flags)
    assert "indexPageUrl (not set)" in flags


def test_item_missing_fields_tracks_per_item_gaps():
    s = _store()
    s.add_item({"title": "A", "url": "https://x.com/1"})
    assert set(s.item_missing_fields(0)) == {"department", "location", "description", "publishedAt"}
    s.update_item(0, {"description": "d", "publishedAt": "2026-01-01", "department": "Eng", "location": "London"})
    assert s.item_missing_fields(0) == []
    assert s.item_count() == 1


def test_item_missing_fields_out_of_range():
    s = _store()
    assert s.item_missing_fields(0) == []
    assert s.item_count() == 0


def test_single_array_no_item_model():
    schema = {
        "type": "object",
        "required": ["items"],
        "properties": {"items": {"type": "array", "items": {"type": "string"}}},
    }
    s = OutputStore(json_schema_to_pydantic(schema, "T"))
    assert s.data == {"items": []}
    assert s.array_field == "items"
    assert s.item_model is None
    ok, _ = s.add_item({"anything": "goes"})
    assert ok is True


ENUM_SCHEMA = {
    "type": "object",
    "required": ["items"],
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["title"],
                "properties": {
                    "title": {"type": "string"},
                    "condition": {
                        "anyOf": [
                            {"type": "string", "enum": ["NEW", "USED", "REFURBISHED"]},
                            {"type": "null"},
                        ]
                    },
                    "extra": {
                        "anyOf": [
                            {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "key": {"type": "string"},
                                        "value": {"type": "string"},
                                    },
                                },
                            },
                            {"type": "null"},
                        ]
                    },
                    "publishedAt": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                },
            },
        },
    },
}


def _enum_store() -> OutputStore:
    return OutputStore(json_schema_to_pydantic(ENUM_SCHEMA, "EnumOutput"))


def test_enum_case_insensitive_coercion():
    s = _enum_store()
    ok, msg = s.add_item({"title": "A", "condition": "Used"})
    assert ok is True, msg
    assert s.data["items"][0]["condition"] == "USED"
    ok, _ = s.add_item({"title": "B", "condition": " refurbished "})
    assert ok is True
    assert s.data["items"][1]["condition"] == "REFURBISHED"


def test_enum_coercion_rejects_genuinely_wrong_value():
    s = _enum_store()
    ok, msg = s.add_item({"title": "A", "condition": "Brand new-ish"})
    assert ok is False
    assert "condition" in msg


def test_string_whitespace_trimmed():
    s = _store()
    ok, _ = s.add_item({"title": "  Padded  ", "url": "https://x.com/u"})
    assert ok is True
    assert s.data["items"][0]["title"] == "Padded"


def test_set_field_enum_coercion():
    schema = {
        "type": "object",
        "required": ["status"],
        "properties": {
            "status": {"type": "string", "enum": ["OPEN", "CLOSED"]},
            "items": {"type": "array", "items": {"type": "object", "properties": {"t": {"type": "string"}}}},
        },
    }
    s = OutputStore(json_schema_to_pydantic(schema, "T"))
    ok, _ = s.set_field("status", "open")
    assert ok is True
    assert s.data["status"] == "OPEN"


def test_update_many_applies_and_reports_failures():
    s = _store()
    s.add_item({"title": "A", "url": "https://x.com/1"})
    s.add_item({"title": "B", "url": "https://x.com/2"})
    ok, msg = s.update_many(
        [
            {"index": 0, "fields": {"publishedAt": "2026-01-01"}},
            {"index": 1, "fields": {"publishedAt": "2026-01-02"}},
            {"index": 9, "fields": {"publishedAt": "x"}},
            "not-a-dict",
        ]
    )
    assert ok is True
    assert "Applied 2 of 4" in msg
    assert "entry 2" in msg and "entry 3" in msg
    assert s.data["items"][0]["publishedAt"] == "2026-01-01"
    assert s.data["items"][1]["publishedAt"] == "2026-01-02"


def test_update_many_rejects_non_list():
    s = _store()
    ok, _ = s.update_many({"index": 0})
    assert ok is False
    ok, _ = s.update_many([])
    assert ok is False


def test_mark_absent_settles_fields():
    s = _store()
    s.set_field("indexPageUrl", "https://example.com/items")
    s.add_item({"title": "A", "url": "https://x.com/1", "description": "d", "department": "Eng", "location": "L"})
    assert any("publishedAt" in e for e in s.empty_fields())
    ok, msg = s.mark_absent("publishedAt", "no date shown anywhere on detail pages")
    assert ok is True and "publishedAt" in msg
    assert not any("publishedAt" in e for e in s.empty_fields())
    assert "publishedAt" not in s.item_missing_fields(0)
    assert s.absent_fields == {"publishedAt": "no date shown anywhere on detail pages"}


def test_mark_absent_rejects_unknown_field_and_missing_reason():
    s = _store()
    ok, msg = s.mark_absent("nonsense", "because")
    assert ok is False and "not a schema field" in msg
    ok, msg = s.mark_absent("publishedAt", "  ")
    assert ok is False and "reason" in msg


def test_mark_absent_accepts_top_level_field():
    s = _store()
    ok, _ = s.mark_absent("indexPageUrl", "checked; no about page")
    assert ok is True
    assert not any("indexPageUrl" in e for e in s.empty_fields())


def test_coverage_summary_groups_fields():
    s = _store()
    s.set_field("indexPageUrl", "https://example.com")
    s.add_item({"title": "A", "url": "https://x.com/1", "description": "d"})
    s.add_item({"title": "B", "url": "https://x.com/2"})
    s.mark_absent("location", "not shown")
    cov = s.coverage_summary()
    assert cov.startswith("Coverage — ")
    assert "items: 2 item(s)" in cov
    assert "title" in cov and "url" in cov
    assert "description 1/2" in cov
    assert "empty on all: department, publishedAt" in cov
    assert "marked absent: location" in cov


def test_coverage_summary_empty_store():
    s = _store()
    cov = s.coverage_summary()
    assert "items: 0 item(s)" in cov
    assert "top-level not set: indexPageUrl" in cov


def test_extra_key_hints_spots_lookalike():
    s = _enum_store()
    s.add_item(
        {
            "title": "A",
            "extra": [{"key": "datePublished", "value": "2026-08-04"}],
        }
    )
    hints = s.extra_key_hints()
    assert any("datePublished" in h and "publishedAt" in h for h in hints)


def test_extra_key_hints_quiet_when_filled_or_absent():
    s = _enum_store()
    s.add_item(
        {
            "title": "A",
            "publishedAt": "2026-08-04",
            "extra": [{"key": "datePublished", "value": "2026-08-04"}],
        }
    )
    assert not any("publishedAt" in h for h in s.extra_key_hints())
    s2 = _enum_store()
    s2.add_item({"title": "B", "extra": [{"key": "datePublished", "value": "x"}]})
    s2.mark_absent("publishedAt", "not published")
    assert not any("publishedAt" in h for h in s2.extra_key_hints())


def test_bool_coerces_to_string_for_plain_str_fields():
    s = _store()
    s.add_item({"title": "A", "url": "https://x.com/1"})
    ok, msg = s.update_item(0, {"description": True})
    assert ok is True, msg
    assert s.data["items"][0]["description"] == "true"
    ok, _ = s.update_item(0, {"description": False})
    assert s.data["items"][0]["description"] == "false"


def test_read_output_paging_windows_the_array():
    s = _store()
    s.set_field("indexPageUrl", "https://example.com")
    for i in range(5):
        s.add_item({"title": f"Item {i}", "url": f"https://x.com/{i}"})

    full = json.loads(s.read_output())
    assert len(full["items"]) == 5 and "_window" not in full

    page = json.loads(s.read_output(offset=1, limit=2))
    assert [j["title"] for j in page["items"]] == ["Item 1", "Item 2"]
    assert "items[1:3] of 5" in page["_window"]
    assert page["indexPageUrl"] == "https://example.com"

    tail = json.loads(s.read_output(offset=4, limit=10))
    assert [j["title"] for j in tail["items"]] == ["Item 4"]


def test_read_output_compact_elides_long_values() -> None:
    s = _store()
    long_desc = ("role details " * 50).strip()
    s.add_item({"title": "Job 0", "url": "https://x.com/0", "description": long_desc})
    s.add_item({"title": "Job 1", "url": "https://x.com/1", "description": long_desc})

    compact = json.loads(s.read_output(compact=True))
    assert compact["items"][0]["description"] == f"<{len(long_desc)} chars>"
    assert compact["items"][0]["title"] == "Job 0"
    assert "2 long value(s)" in compact["_elided"]

    full = json.loads(s.read_output())
    assert full["items"][0]["description"] == long_desc
    assert "_elided" not in full

    kept = json.loads(s.read_output(compact=True, fields=["description"]))
    assert kept["items"][1]["description"] == long_desc

    one = json.loads(s.read_output(compact=True, index=1))
    assert [j["title"] for j in one["items"]] == ["Job 1"]
    assert one["items"][0]["description"] == long_desc
    assert "items[1] of 2 in full" in one["_window"]

    oob = json.loads(s.read_output(compact=True, index=9))
    assert "out of range" in oob["_window"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))


SCALAR_SCHEMA = {
    "type": "object",
    "required": ["headlines"],
    "properties": {
        "headlines": {"type": "array", "items": {"type": "string"}},
    },
}


def _scalar_store() -> OutputStore:
    return OutputStore(json_schema_to_pydantic(SCALAR_SCHEMA, "TaskOutput"))


def test_scalar_array_accepts_plain_values() -> None:
    store = _scalar_store()
    ok, msg = store.add_item("First headline")
    assert ok, msg
    ok, msg = store.add_item("Second headline")
    assert ok, msg
    data = json.loads(store.read_output())
    assert data["headlines"] == ["First headline", "Second headline"]
    store.output_model.model_validate(data)


def test_scalar_array_unwraps_single_key_object() -> None:
    """A model given no object schema tends to invent a wrapper key; the value it
    clearly meant must land as the plain item the schema demands."""
    store = _scalar_store()
    ok, msg = store.add_item({"headline": "Wrapped headline"})
    assert ok, msg
    data = json.loads(store.read_output())
    assert data["headlines"] == ["Wrapped headline"]
    store.output_model.model_validate(data)


def test_scalar_array_rejects_multi_key_object_and_wrong_type() -> None:
    store = _scalar_store()
    ok, msg = store.add_item({"a": 1, "b": 2})
    assert not ok and "plain values" in msg
    ok, msg = store.add_item({"headline": 42})
    assert not ok and "must be" in msg


def _permissive_store() -> OutputStore:
    # No additionalProperties clause — the shape that let a live budget-salvage
    # ship url/text scaffolding alongside the schema fields.
    schema = {
        "type": "object",
        "required": ["staff"],
        "properties": {
            "staff": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["name", "role"],
                    "properties": {
                        "name": {"type": "string"},
                        "role": {"type": "string"},
                    },
                },
            }
        },
    }
    return OutputStore(json_schema_to_pydantic(schema, "StaffOutput"))


def test_final_output_strips_extra_item_fields() -> None:
    # Extra keys stay in the live store (the completeness gate hints off
    # them), but the published answer must honour the schema's shape exactly.
    s = _permissive_store()
    ok, _ = s.add_item(
        {"name": "A", "role": "Glazier", "url": "http://x/1", "text": "raw page"}
    )
    assert ok
    live = json.loads(s.read_output())
    published = json.loads(s.final_output())
    assert "url" in live["staff"][0] and "text" in live["staff"][0]
    assert set(published["staff"][0]) == {"name", "role"}
    assert published["staff"][0]["name"] == "A"


def test_final_output_matches_read_output_when_items_are_clean() -> None:
    s = _store()
    ok, _ = s.add_item({"title": "A", "url": "http://x/1"})
    assert ok
    assert json.loads(s.final_output()) == json.loads(s.read_output())


CONDITIONAL_SCHEMA = {
    "type": "object",
    "required": ["found"],
    "properties": {
        "found": {"type": "boolean"},
        "reason": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "title": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "tags": {"anyOf": [{"type": "array", "items": {"type": "string"}}, {"type": "null"}]},
    },
    "if": {"properties": {"found": {"const": False}}},
    "then": {"required": ["reason"]},
    "else": {"not": {"required": ["reason"]}},
}


def _conditional_store():
    return OutputStore(
        json_schema_to_pydantic(CONDITIONAL_SCHEMA, "Cond"),
        schema_directives(CONDITIONAL_SCHEMA),
    )


def test_conditional_field_is_not_chased_on_the_branch_that_excuses_it():
    s = _conditional_store()
    s.set_field("found", True)
    s.set_field("title", "Arize")
    s.add_item("ai")
    assert s.empty_fields() == []
    assert "not applicable: reason" in s.coverage_summary()


def test_conditional_field_is_chased_on_the_branch_that_needs_it():
    s = _conditional_store()
    s.set_field("found", False)
    assert any("reason" in e for e in s.empty_fields())


def test_conditional_field_flips_back_when_the_answer_moves():
    """The rule is evaluated against live data, not frozen at construction."""
    s = _conditional_store()
    s.set_field("found", False)
    assert any("reason" in e for e in s.empty_fields())
    s.set_field("found", True)
    assert not any("reason" in e for e in s.empty_fields())


def test_store_without_directives_behaves_as_before():
    s = OutputStore(json_schema_to_pydantic(CONDITIONAL_SCHEMA, "Plain"))
    s.set_field("found", True)
    assert any("reason" in e for e in s.empty_fields())


def test_seed_prefills_through_the_validated_writers():
    s = _conditional_store()
    applied, skipped = s.seed({"title": "Arize AI", "tags": ["ai", "observability"]})
    assert applied == ["title", "tags"]
    assert skipped == []
    assert json.loads(s.read_output())["title"] == "Arize AI"
    assert not any("title" in e for e in s.empty_fields())


def test_seed_reports_what_it_could_not_apply_and_never_raises():
    s = _conditional_store()
    applied, skipped = s.seed(
        {"title": {"not": "a string"}, "nosuchfield": "x", "found": None}
    )
    assert applied == []
    assert skipped == ["title"]
    assert json.loads(s.read_output())["title"] is None


def test_seeded_array_items_are_validated_one_by_one():
    s = _conditional_store()
    applied, skipped = s.seed({"tags": ["ai", {"wrapped": "ok"}]})
    assert applied == ["tags"]
    assert json.loads(s.read_output())["tags"] == ["ai", "ok"]


def test_update_item_refuses_to_put_an_object_into_a_list_of_values():
    """The finsmes corruption: a merge onto a list[str] element stored the raw
    dict and reported success, so the run failed its own schema check."""
    s = _conditional_store()
    s.add_item("Purple Ventures")
    ok, _ = s.update_item(0, {"item": "BlackWood Ventures"})
    assert ok is True
    assert json.loads(s.read_output())["tags"] == ["BlackWood Ventures"]
    ok, msg = s.update_item(0, {"a": "x", "b": "y"})
    assert ok is False and "plain values" in msg
    assert json.loads(s.read_output())["tags"] == ["BlackWood Ventures"]


def test_update_item_keeps_a_scalar_list_schema_valid():
    s = _conditional_store()
    s.set_field("found", True)
    s.add_item("one")
    s.update_item(0, {"value": "two"})
    s.output_model.model_validate(json.loads(s.read_output()))
