"""Tests for the schema-validated output store (app/agent/output_store.py).

Pure — no browser-use import — so they run in the repo's local venv.
"""

import json

import pytest

from app.agent.output_store import OutputStore
from app.agent.schema import json_schema_to_pydantic

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["jobs", "careersPageUrl"],
    "properties": {
        "jobs": {
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
                    "postedAt": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                },
            },
        },
        "careersPageUrl": {"type": "string"},
    },
}


def _store() -> OutputStore:
    return OutputStore(json_schema_to_pydantic(SCHEMA, "TaskOutput"))


def test_empty_from_schema():
    s = _store()
    assert s.data == {"jobs": [], "careersPageUrl": None}
    assert s.array_field == "jobs"
    assert s.item_model is not None
    assert s.is_empty() is True
    assert json.loads(s.read_output()) == {"jobs": [], "careersPageUrl": None}


def test_add_item_valid_fills_optional_fields_as_null():
    s = _store()
    ok, msg = s.add_item({"title": "Engineer", "url": "https://x/1"})
    assert ok is True
    assert "#0" in msg
    job = s.data["jobs"][0]
    assert job == {
        "title": "Engineer",
        "department": None,
        "location": None,
        "url": "https://x/1",
        "description": None,
        "postedAt": None,
    }
    assert s.is_empty() is False


def test_add_item_missing_required_rejected():
    s = _store()
    ok, msg = s.add_item({"title": "No URL"})
    assert ok is False
    assert "url" in msg
    assert s.data["jobs"] == []


def test_add_item_extra_field_rejected():
    s = _store()
    ok, msg = s.add_item({"title": "X", "url": "u", "salary": "lots"})
    assert ok is False
    assert s.data["jobs"] == []


def test_add_item_wrong_type_rejected():
    s = _store()
    ok, msg = s.add_item({"title": 123, "url": "u"})
    assert ok is False


def test_update_item_enriches_stub():
    s = _store()
    s.add_item({"title": "Engineer", "url": "https://x/1"})
    ok, msg = s.update_item(0, {"description": "Build things", "postedAt": "2026-01-02"})
    assert ok is True
    assert s.data["jobs"][0]["description"] == "Build things"
    assert s.data["jobs"][0]["postedAt"] == "2026-01-02"
    assert s.data["jobs"][0]["title"] == "Engineer"


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
    assert s.data["jobs"][0]["title"] == "Engineer"


def test_set_field_valid():
    s = _store()
    ok, msg = s.set_field("careersPageUrl", "https://co/careers")
    assert ok is True
    assert s.data["careersPageUrl"] == "https://co/careers"


def test_set_field_wrong_type_rejected():
    s = _store()
    ok, msg = s.set_field("careersPageUrl", {"not": "a string"})
    assert ok is False
    assert s.data["careersPageUrl"] is None


def test_set_field_unknown_key_rejected():
    s = _store()
    ok, msg = s.set_field("nope", "x")
    assert ok is False
    assert "not an output field" in msg


def test_set_field_on_array_rejected():
    s = _store()
    ok, msg = s.set_field("jobs", [])
    assert ok is False
    assert "add_item" in msg


def test_search_output():
    s = _store()
    s.add_item({"title": "Backend Engineer", "url": "https://x/1"})
    s.add_item({"title": "Designer", "url": "https://x/2"})
    s.set_field("careersPageUrl", "https://co/careers")
    res = json.loads(s.search_output("engineer"))
    assert "jobs" in res
    assert len(res["jobs"]) == 1
    assert res["jobs"][0]["index"] == 0
    res2 = json.loads(s.search_output("careers"))
    assert res2.get("careersPageUrl") == "https://co/careers"
    assert json.loads(s.search_output("zzz-not-present")) == {}


def test_empty_fields_flags_missing_details():
    s = _store()
    s.add_item({"title": "A", "url": "u1"})
    s.add_item({"title": "B", "url": "u2", "description": "has one"})
    flags = s.empty_fields()
    joined = " | ".join(flags)
    assert "careersPageUrl (not set)" in flags
    assert "description — empty on 1 of 2 jobs" in joined
    assert "postedAt — empty on 2 of 2 jobs" in joined


def test_empty_fields_empty_list():
    s = _store()
    flags = s.empty_fields()
    assert any("jobs (list is empty)" in f for f in flags)
    assert "careersPageUrl (not set)" in flags


def test_item_missing_fields_tracks_per_item_gaps():
    s = _store()
    s.add_item({"title": "A", "url": "u1"})
    assert set(s.item_missing_fields(0)) == {"department", "location", "description", "postedAt"}
    s.update_item(0, {"description": "d", "postedAt": "2026-01-01", "department": "Eng", "location": "London"})
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


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
