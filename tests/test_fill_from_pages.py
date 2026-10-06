"""Values a page states in its prose reach the answer, and absence has to be looked for.

A benchmark run scored 53.5% because read_pages showed the model only each role's
list-row snippet, the draft dropped the company held in JSON-LD, the store refused
SALARIED because the page says "Full time", and mark_absent passed once the pages had
merely been opened. These replay two real Marshmallow role pages through each step.
"""

from __future__ import annotations

import json
import types
from pathlib import Path

import pytest
from browser_use import Tools
from browser_use.filesystem.file_system import FileSystem

from openbrowse.agent import tools as tools_mod
from openbrowse.agent.output_store import OutputStore
from openbrowse.agent.schema import json_schema_to_pydantic

ROOT = Path(__file__).resolve().parent.parent
PAGES = json.loads((Path(__file__).parent / "fixtures" / "marshmallow_pages.json").read_text())
CLAIMS, HEAD = PAGES


def _store() -> OutputStore:
    spec = json.loads((ROOT / "benchmark.json").read_text())
    return OutputStore(json_schema_to_pydantic(spec["outputSchema"], "Out"))


def _drafted(store: OutputStore) -> dict:
    clipboard: dict = {tools_mod._READ_PAGES_KEY: PAGES, "_visited": set()}
    for page in PAGES:
        ok, msg = store.add_item(tools_mod._draft_row(store, page))
        assert ok, msg
        clipboard["_visited"].add(tools_mod._norm_url(page["url"]))
    tools_mod._extend_evidence_corpus(clipboard, {p["url"]: p for p in PAGES})
    store.evidence_check = lambda value: tools_mod._evidence_contains(
        clipboard.get("_evidence_corpus", ""), value
    )
    return clipboard


def test_the_draft_takes_the_company_from_nested_json_ld():
    row = tools_mod._draft_row(_store(), CLAIMS)
    assert row["companyName"] == "marshmallow"
    assert row["companyUrl"] == "https://www.marshmallow.co"
    assert "companyDescription" not in row


def test_the_draft_reads_a_visible_deadline_label_as_the_closing_date():
    row = tools_mod._draft_row(_store(), CLAIMS)
    assert row["expiresAt"] == "8 October 2026 at 23:00 BST"
    assert "expiresAt" not in tools_mod._draft_row(_store(), HEAD)


def test_a_link_from_structured_data_never_lands_in_a_prose_field():
    page = dict(CLAIMS, jsonld=dict(CLAIMS["jsonld"]))
    page["jsonld"]["hiringOrganization"] = {
        "name": "marshmallow",
        "logo": "https://cdn.example.com/logo.png",
    }
    row = tools_mod._draft_row(_store(), page)
    assert not any(
        str(row.get(f, "")).startswith("http") for f in ("companyDescription", "companyName")
    )


def test_an_enum_the_page_words_differently_is_accepted_with_its_page_line():
    store = _store()
    _drafted(store)
    refused, msg = store.update_item(0, {"compensationType": "SALARIED"})
    assert not refused and "sources=" in msg
    ok, msg = store.update_item(0, {"compensationType": "SALARIED"}, {"compensationType": "Full time"})
    assert ok, msg
    ok, msg = store.update_item(1, {"ir35Status": "INSIDE_IR35"}, {"ir35Status": "inside IR35"})
    assert not ok and "not on any read page" in msg


def test_update_items_carries_sources_per_entry():
    store = _store()
    _drafted(store)
    ok, msg = store.update_many(
        [{"index": 0, "fields": {"compensationType": "SALARIED"}, "sources": {"compensationType": "Full time"}}]
    )
    assert ok, msg


class _FakeModel:
    """Answers each page the way a model reading it would, plus one invention."""

    def __init__(self, fail_for: str | None = None):
        self.calls: list[str] = []
        self.fail_for = fail_for

    async def ainvoke(self, messages, output_format=None, **_):
        prompt = messages[-1].content
        self.calls.append(prompt)
        if self.fail_for and self.fail_for in prompt:
            raise TimeoutError("model call timed out")
        head = HEAD["url"] in prompt
        data = {
            "visaSponsorship": "(We cannot offer visa sponsorship for this role)" if head else None,
            "seniority": "Head" if head else None,
            "skills": ["Python", "Snowflake", "Kubernetes"] if head else ["communication skills"],
            "compensationType": "SALARIED",
            "companyDescription": "We started building Marshmallow in 2017.",
            "ir35Status": "OUTSIDE_IR35",
            "locationType": "HYBRID" if head else None,
            "page_quotes": [
                {"field": "compensationType", "quote": "Full time"},
                {"field": "ir35Status", "quote": "Outside IR35 day rate"},
                {"field": "locationType", "quote": "12m FTC - (London, hybrid 3 days a week)"},
            ],
        }
        known = set(output_format.model_fields)
        return types.SimpleNamespace(
            completion=output_format(**{k: v for k, v in data.items() if k in known})
        )


def _fill_tools(store: OutputStore, clipboard: dict) -> Tools:
    tools = Tools()
    tools_mod.register_tab_tools(tools, object(), clipboard, store, None)
    tools_mod.register_output_store_tools(tools, store, clipboard)
    store.evidence_check = lambda value: tools_mod._evidence_contains(
        clipboard.get("_evidence_corpus", ""), value
    )
    return tools


async def _call(tools: Tools, name: str, fs: FileSystem, **kwargs):
    entry = tools.registry.registry.actions[name]
    extra = {"page_extraction_llm": kwargs.pop("llm")} if "llm" in kwargs else {}
    return await entry.function(params=entry.param_model(**kwargs), file_system=fs, **extra)


@pytest.fixture
def fs(tmp_path):
    return FileSystem(tmp_path)


async def test_fill_from_pages_writes_what_each_page_states_and_refuses_the_rest(fs):
    store = _store()
    clipboard = _drafted(store)
    tools = _fill_tools(store, clipboard)
    model = _FakeModel()
    result = await _call(tools, "fill_from_pages", fs, llm=model)
    assert len(model.calls) == 2
    claims, head = store.data["jobs"]
    assert head["visaSponsorship"] == "(We cannot offer visa sponsorship for this role)"
    assert head["seniority"] == "Head"
    assert head["skills"] == ["Python", "Snowflake"]
    assert claims["skills"] == ["communication skills"]
    assert claims["compensationType"] == head["compensationType"] == "SALARIED"
    assert head["locationType"] == "HYBRID"
    assert claims["companyDescription"] == "We started building Marshmallow in 2017."
    assert claims["ir35Status"] is None and head["ir35Status"] is None
    text = result.extracted_content
    assert "visaSponsorship on 1 of" in text and "ir35Status" in text
    assert "Found on no page" in text and "ir35Status" in text.split("Found on no page")[1]


async def test_a_field_cannot_be_marked_absent_before_its_pages_are_read_for_it(fs):
    store = _store()
    clipboard = _drafted(store)
    tools = _fill_tools(store, clipboard)
    early = await _call(tools, "mark_absent", fs, field="ir35Status", reason="not on the pages")
    assert early.error and "fill_from_pages()" in early.error
    await _call(tools, "fill_from_pages", fs, llm=_FakeModel())
    later = await _call(tools, "mark_absent", fs, field="ir35Status", reason="not on the pages")
    assert not later.error, later.error


async def test_a_page_the_model_fails_on_is_retried_once_then_counted_as_looked_at(fs):
    store = _store()
    clipboard = _drafted(store)
    tools = _fill_tools(store, clipboard)
    model = _FakeModel(fail_for=CLAIMS["url"])
    first = await _call(tools, "fill_from_pages", fs, llm=model)
    assert "could not be read this time" in first.extracted_content
    assert tools_mod._fill_pending(store, clipboard, "ir35Status") == 1
    await _call(tools, "fill_from_pages", fs, llm=model)
    assert tools_mod._fill_pending(store, clipboard, "ir35Status") == 0
    third = await _call(tools, "fill_from_pages", fs, llm=model)
    assert "nothing left to read" in third.extracted_content


async def test_fill_from_pages_needs_read_pages_first(fs):
    store = _store()
    tools = _fill_tools(store, {})
    result = await _call(tools, "fill_from_pages", fs, llm=_FakeModel())
    assert result.error and "read_pages first" in result.error


def test_the_guidance_sends_empty_fields_to_fill_from_pages_before_absence():
    from openbrowse.agent.runner import _TOOLS_EASIEST_EXTENSION

    assert "fill_from_pages()" in _TOOLS_EASIEST_EXTENSION
    assert _TOOLS_EASIEST_EXTENSION.index("fill_from_pages()") < _TOOLS_EASIEST_EXTENSION.index(
        "mark_absent any field"
    )
    assert "page['link_text']" not in _TOOLS_EASIEST_EXTENSION


async def test_an_item_whose_url_was_rewritten_to_the_ats_link_still_finds_its_page(fs):
    store = _store()
    clipboard = _drafted(store)
    claims_id = "fd0acc52-8604-4648-8575-631bd155a1c0"
    clipboard[tools_mod._READ_PAGES_KEY] = [
        dict(CLAIMS, url=f"https://www.marshmallow.com/jobs?ashby_jid={claims_id}#openings"),
        HEAD,
    ]
    assert tools_mod._fill_pending(store, clipboard, "ir35Status") == 2
    tools = _fill_tools(store, clipboard)
    await _call(tools, "fill_from_pages", fs, llm=_FakeModel())
    assert store.data["jobs"][0]["compensationType"] == "SALARIED"
    assert tools_mod._fill_pending(store, clipboard, "ir35Status") == 0
