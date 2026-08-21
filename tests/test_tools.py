"""Tool registration tests (no live API calls)."""

import pytest
from browser_use import ActionResult, Tools


def test_register_fetch_tool() -> None:
    tools = Tools()
    from openbrowse.agent.tools import register_fetch_tool

    register_fetch_tool(tools)
    assert "http_fetch" in tools.registry.registry.actions


def test_register_code_tools() -> None:
    tools = Tools()
    from openbrowse.agent.tools import register_code_tools

    register_code_tools(tools)
    actions = tools.registry.registry.actions
    assert "write_code_file" not in actions
    assert "run_code_file" in actions
    assert "run_python" not in actions


def test_normalise_py_name() -> None:
    from openbrowse.agent.tools import _normalise_py_name

    assert _normalise_py_name("extract") == "extract.py"
    assert _normalise_py_name("extract.py") == "extract.py"
    assert _normalise_py_name("a/b/scrape") == "scrape.py"
    assert _normalise_py_name("weird name!.txt") == "weird_name_.txt.py"
    assert _normalise_py_name("") == "script.py"


def test_item_url_field_prefers_detail_over_company() -> None:
    from openbrowse.agent.output_store import OutputStore
    from openbrowse.agent.schema import json_schema_to_pydantic
    from openbrowse.agent.tools import _item_url_field

    schema = {
        "type": "object",
        "properties": {
            "items": {
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
    from openbrowse.agent.output_store import OutputStore
    from openbrowse.agent.schema import json_schema_to_pydantic
    from openbrowse.agent.tools import _item_url_field

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


def test_norm_url() -> None:
    from openbrowse.agent.tools import _norm_url

    a = _norm_url("https://www.example.com/listings?embed_id=ABC#section")
    b = _norm_url("https://www.example.com/listings?embed_id=ABC/")
    c = _norm_url("HTTPS://WWW.EXAMPLE.COM/listings?embed_id=ABC")
    assert a == b == c == "https://www.example.com/listings?embed_id=abc"
    assert _norm_url("") == ""


def test_bare_stub_count_counts_unopened_contentless_items() -> None:
    from openbrowse.agent.tools import _norm_url, _bare_stub_count

    store = _items_store()
    store.add_item({"title": "A", "sourceUrl": "https://x/a"})
    store.add_item({"title": "B", "sourceUrl": "https://x/b"})
    store.add_item({"title": "C"})

    assert _bare_stub_count(store, set()) == 2
    assert _bare_stub_count(store, {_norm_url("https://x/a")}) == 1
    assert _bare_stub_count(store, {_norm_url("https://x/a"), _norm_url("https://x/b")}) == 0


def test_item_with_description_is_not_a_stub() -> None:
    from openbrowse.agent.tools import _bare_stub_count, _is_bare_stub

    store = _items_store()
    long_desc = "We are hiring. " * 20
    filled = {"title": "A", "sourceUrl": "https://x/a", "description": long_desc}
    bare = {"title": "B", "sourceUrl": "https://x/b"}
    assert _is_bare_stub(store, filled, set()) is False
    assert _is_bare_stub(store, bare, set()) is True
    store.add_item(filled)
    store.add_item(bare)
    assert _bare_stub_count(store, set()) == 1


def test_bare_stub_count_no_url_field() -> None:
    from openbrowse.agent.output_store import OutputStore
    from openbrowse.agent.schema import json_schema_to_pydantic
    from openbrowse.agent.tools import _bare_stub_count

    schema = {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"name": {"anyOf": [{"type": "string"}, {"type": "null"}]}},
                },
            }
        },
    }
    store = OutputStore(json_schema_to_pydantic(schema, "T"))
    store.add_item({"name": "x"})
    assert _bare_stub_count(store, set()) == 0


class _FakeFileSystem:
    def __init__(self) -> None:
        import tempfile
        from pathlib import Path

        self.files: dict[str, str] = {}
        self._dir = Path(tempfile.mkdtemp())

    def get_dir(self):
        return self._dir

    async def write_file(self, name: str, content: str) -> None:
        self.files[name] = content

    def get_file(self, name: str):
        if name not in self.files:
            return None
        content = self.files[name]

        class _F:
            def read(self_inner) -> str:
                return content

        return _F()


def _items_store():
    from openbrowse.agent.output_store import OutputStore
    from openbrowse.agent.schema import json_schema_to_pydantic

    schema = {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["title"],
                    "properties": {
                        "title": {"type": "string"},
                        "sourceUrl": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                        "description": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                    },
                },
            }
        },
    }
    return OutputStore(json_schema_to_pydantic(schema, "T"))


def test_register_output_store_tools_actions() -> None:
    from openbrowse.agent.tools import register_output_store_tools

    tools = Tools()
    register_output_store_tools(tools, _items_store(), {})
    actions = tools.registry.registry.actions
    for name in (
        "add_item",
        "update_item",
        "update_items",
        "set_field",
        "mark_absent",
        "read_output",
        "search_output",
        "add_items_from_file",
        "update_items_from_file",
    ):
        assert name in actions, name


def test_register_tab_tools_includes_read_pages() -> None:
    from openbrowse.agent.tools import TabManager, register_tab_tools

    tools = Tools()
    register_tab_tools(tools, TabManager(session=None), {})
    actions = tools.registry.registry.actions
    assert "read_pages" in actions
    assert "open_tabs" in actions


def test_parse_jsonld_blobs() -> None:
    import json as _json

    from openbrowse.agent.tools import _parse_jsonld_blobs

    assert _parse_jsonld_blobs(None) is None
    assert _parse_jsonld_blobs([]) is None
    assert _parse_jsonld_blobs(["{not json"]) is None
    plain = _parse_jsonld_blobs([_json.dumps({"@type": "Organization", "name": "X"})])
    assert plain["name"] == "X"
    posting = _parse_jsonld_blobs(
        [
            _json.dumps({"@type": "Organization"}),
            _json.dumps([{"@type": "JobPosting", "datePublished": "2026-08-04"}]),
        ]
    )
    assert posting["datePublished"] == "2026-08-04"


def test_stub_block_msg_throttles_unvisited_listing_items() -> None:
    from openbrowse.agent.tools import _MAX_UNVISITED_STUBS, _stub_block_msg

    store = _items_store()
    clipboard: dict = {}
    for i in range(_MAX_UNVISITED_STUBS):
        assert (
            _stub_block_msg(
                store, clipboard, {"title": f"J{i}", "sourceUrl": f"https://x.com/{i}"}
            )
            is None
        )
        store.add_item({"title": f"J{i}", "sourceUrl": f"https://x.com/{i}"})
    blocked = _stub_block_msg(
        store, clipboard, {"title": "J9", "sourceUrl": "https://x.com/9"}
    )
    assert blocked is not None and "read_pages" in blocked
    clipboard["_visited"] = {"https://x.com/9"}
    assert (
        _stub_block_msg(store, clipboard, {"title": "J9", "sourceUrl": "https://x.com/9"})
        is None
    )


async def test_store_bridge_writes_and_mirrors() -> None:
    import asyncio

    from openbrowse.agent.tools import _store_bridge

    store = _items_store()
    fs = _FakeFileSystem()
    clipboard: dict = {"_visited": {"https://x.com/1"}}
    bridge = _store_bridge(store, clipboard, fs)

    msg = await bridge["add_item"](
        {"title": "A", "sourceUrl": "https://x.com/1", "description": "d" * 200}
    )
    assert "Added item #0" in msg
    assert (fs.get_dir() / "output.json").exists()
    await asyncio.sleep(0)
    assert "output.json" in fs.files

    msg = await bridge["update_item"](0, {"description": "e" * 200})
    assert "Updated item #0" in msg
    msg = await bridge["update_items"]([{"index": 0, "fields": {"title": "A2"}}])
    assert "Applied 1 of 1" in msg
    msg = await bridge["mark_absent"]("description", "not published")
    assert "already settled" in msg
    out = bridge["read_output"]()
    assert isinstance(out, dict)
    assert out["items"][0]["title"] == "A2"
    awaited = await bridge["read_output"]()
    assert awaited["items"][0]["title"] == "A2"
    assert bridge["coverage"]().startswith("Coverage — ")


async def test_store_bridge_respects_stub_limit() -> None:
    from openbrowse.agent.tools import _MAX_UNVISITED_STUBS, _store_bridge

    store = _items_store()
    fs = _FakeFileSystem()
    bridge = _store_bridge(store, {}, fs)
    for i in range(_MAX_UNVISITED_STUBS):
        await bridge["add_item"]({"title": f"J{i}", "sourceUrl": f"https://x.com/{i}"})
    msg = await bridge["add_item"]({"title": "J9", "sourceUrl": "https://x.com/9"})
    assert "Slow down" in msg
    assert store.item_count() == _MAX_UNVISITED_STUBS


async def test_read_pages_impl_waves_retry_and_visited(monkeypatch) -> None:
    import openbrowse.agent.tools as tools_mod

    spawned: list[str] = []
    closed: list[str] = []
    fail_once = {"https://x.com/2"}

    focused: list[str] = []

    async def fake_iframe_targets(session):
        return []

    async def fake_spawn(session, url):
        spawned.append(url)
        return f"tid-{len(spawned)}"

    async def fake_close(session, tid):
        closed.append(tid)

    async def fake_focus(session, tid):
        focused.append(tid)

    sole_flags: list[bool] = []

    async def fake_read_one(
        session, url, tid, url_contains, claimed, baseline, allow_sole_candidate=False,
        sibling_urls=None,
    ):
        sole_flags.append(allow_sole_candidate)
        if url in fail_once:
            fail_once.discard(url)
            return {"url": url, "error": "no readable text rendered"}
        return {"url": url, "title": "t", "text": "body", "jsonld": None, "links": []}

    monkeypatch.setattr(tools_mod, "_iframe_targets", fake_iframe_targets)
    monkeypatch.setattr(tools_mod, "_spawn_tab", fake_spawn)
    monkeypatch.setattr(tools_mod, "_close_spawned_tab", fake_close)
    monkeypatch.setattr(tools_mod, "_focus_target", fake_focus)
    monkeypatch.setattr(tools_mod, "_read_one_page", fake_read_one)

    urls = [f"https://x.com/{i}" for i in range(5)]
    clipboard: dict = {}
    pages = await tools_mod._read_pages_impl(None, urls, "embed", clipboard, concurrency=2)

    assert [p["url"] for p in pages] == urls
    assert all(not p.get("error") for p in pages)
    assert len(spawned) == 6
    assert len(closed) == 6
    assert len(clipboard["_visited"]) == 5
    assert clipboard["_read_failed"] == set()
    assert len(focused) >= 6
    assert sole_flags == [False, False, False, False, True, True]


async def test_read_pages_impl_records_failures_and_retries_missing_jsonld(
    monkeypatch,
) -> None:
    import openbrowse.agent.tools as tools_mod

    reads: list[str] = []
    dead = "https://x.com/dead"
    slow_ld = "https://x.com/slow"

    async def fake_iframe_targets(session):
        return []

    async def fake_spawn(session, url):
        return "tid"

    async def fake_close(session, tid):
        pass

    async def fake_focus(session, tid):
        pass

    async def fake_read_one(
        session, url, tid, url_contains, claimed, baseline, allow_sole_candidate=False,
        sibling_urls=None,
    ):
        reads.append(url)
        if url == dead:
            return {"url": url, "error": "no embedded panel matching 'embed' rendered"}
        jsonld = {"datePublished": "2026-08-04"}
        if url == slow_ld and reads.count(url) < 2:
            jsonld = None
        return {"url": url, "text": "body " * 60, "jsonld": jsonld, "links": []}

    monkeypatch.setattr(tools_mod, "_iframe_targets", fake_iframe_targets)
    monkeypatch.setattr(tools_mod, "_spawn_tab", fake_spawn)
    monkeypatch.setattr(tools_mod, "_close_spawned_tab", fake_close)
    monkeypatch.setattr(tools_mod, "_focus_target", fake_focus)
    monkeypatch.setattr(tools_mod, "_read_one_page", fake_read_one)

    urls = ["https://x.com/ok", slow_ld, dead]
    clipboard: dict = {}
    pages = await tools_mod._read_pages_impl(None, urls, "embed", clipboard, concurrency=4)

    by_url = {p["url"]: p for p in pages}
    assert by_url[dead].get("error")
    assert by_url[slow_ld]["jsonld"] == {"datePublished": "2026-08-04"}
    assert tools_mod._norm_url(dead) in clipboard["_read_failed_frame"]
    assert tools_mod._norm_url(dead) not in clipboard["_read_failed"]
    assert tools_mod._norm_url(slow_ld) in clipboard["_visited"]


def _draft_store():
    from openbrowse.agent.output_store import OutputStore
    from openbrowse.agent.schema import json_schema_to_pydantic

    schema = {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["title"],
                    "properties": {
                        "title": {"type": "string"},
                        "sourceUrl": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                        "description": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                        "sellerDescription": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                        "publishedAt": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                        "condition": {
                            "anyOf": [
                                {"type": "string", "enum": ["NEW", "USED"]},
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
                    },
                },
            }
        },
    }
    return OutputStore(json_schema_to_pydantic(schema, "T"))


def test_draft_row_maps_jsonld_deterministically() -> None:
    from openbrowse.agent.tools import _draft_row

    store = _draft_store()
    page = {
        "url": "https://x.com/listings?id=abc12345",
        "title": "Widget One | Store",
        "text": "t" * 500,
        "jsonld": {
            "@type": "Product",
            "title": "Widget One",
            "description": "<p>Great &amp; sturdy.</p><p>Second para.</p>",
            "datePublished": "2026-08-04",
            "employmentType": "FULL_TIME",
            "validThrough": "2026-12-01",
        },
    }
    row = _draft_row(store, page)
    assert row["sourceUrl"] == "https://x.com/listings?id=abc12345"
    assert row["title"] == "Widget One"
    assert row["publishedAt"] == "2026-08-04"
    assert "Great & sturdy." in row["description"]
    assert "<p>" not in row["description"]
    assert "sellerDescription" not in row
    extra_keys = {e["key"] for e in row.get("extra") or []}
    assert "validThrough" in extra_keys
    assert "condition" not in row

    ok, msg = store.add_item(row)
    assert ok is True, msg


def test_draft_row_falls_back_to_page_text_and_invents_nothing() -> None:
    from openbrowse.agent.tools import _draft_row

    store = _draft_store()
    page = {"url": "https://x.com/a", "title": "Bare page", "text": "body " * 100, "jsonld": None}
    row = _draft_row(store, page)
    assert row["sourceUrl"] == "https://x.com/a"
    assert row["title"] == "Bare page"
    assert row["description"].startswith("body")
    assert "publishedAt" not in row and "condition" not in row


def test_strip_html_preserves_paragraphs() -> None:
    from openbrowse.agent.tools import _strip_html

    out = _strip_html("<p>One</p><p>Two &amp; three</p><br>Four")
    assert "One" in out and "Two & three" in out
    assert "<" not in out
    assert "\n" in out


def test_awaitable_helpers_work_with_and_without_await() -> None:
    import asyncio

    from openbrowse.agent.tools import _AwaitableStr, _awaitable

    async def _check():
        s = _AwaitableStr("hello")
        assert s == "hello"
        assert await s == "hello"
        d = _awaitable({"a": 1})
        assert d["a"] == 1
        assert (await d)["a"] == 1
        lst = _awaitable([1, 2])
        assert lst[0] == 1
        assert (await lst) == [1, 2]

    asyncio.run(_check())


async def test_save_json_persists_without_await(tmp_path) -> None:
    from browser_use import Tools

    from openbrowse.agent.tools import register_code_tools

    class _DirFileSystem(_FakeFileSystem):
        def __init__(self, base) -> None:
            super().__init__()
            self._base = base

        def get_dir(self):
            return self._base

    tools = Tools()
    register_code_tools(tools, {}, _jobs_store_or_none())
    fs = _DirFileSystem(tmp_path)
    entry = tools.registry.registry.actions["run_code_file"]
    params = entry.param_model(
        name="t", code="x = save_json({'k': 1}, 'made.json')\nprint(read_json('made.json'))"
    )
    import types

    result = await entry.function(
        params=params, browser_session=types.SimpleNamespace(), file_system=fs
    )
    assert not result.error, result.error
    assert (tmp_path / "made.json").exists()
    assert "'k': 1" in result.extracted_content


def _jobs_store_or_none():
    return None


async def test_read_pages_impl_reports_progress(monkeypatch) -> None:
    import openbrowse.agent.tools as tools_mod

    async def fake_iframe_targets(session):
        return []

    async def fake_spawn(session, url):
        return "tid"

    async def fake_close(session, tid):
        pass

    async def fake_focus(session, tid):
        pass

    async def fake_read_one(
        session, url, tid, url_contains, claimed, baseline, allow_sole_candidate=False,
        sibling_urls=None,
    ):
        return {"url": url, "text": "body " * 60, "jsonld": None, "links": []}

    monkeypatch.setattr(tools_mod, "_iframe_targets", fake_iframe_targets)
    monkeypatch.setattr(tools_mod, "_spawn_tab", fake_spawn)
    monkeypatch.setattr(tools_mod, "_close_spawned_tab", fake_close)
    monkeypatch.setattr(tools_mod, "_focus_target", fake_focus)
    monkeypatch.setattr(tools_mod, "_read_one_page", fake_read_one)

    events: list[str] = []

    async def progress(msg: str) -> None:
        events.append(msg)

    urls = [f"https://x.com/{i}" for i in range(5)]
    await tools_mod._read_pages_impl(None, urls, None, {}, concurrency=2, progress=progress)
    assert any("3 wave(s)" in e for e in events)
    assert sum(1 for e in events if "wave " in e) == 3


def _wave_fakes(monkeypatch, read_one, home="home-tid"):
    import types

    import openbrowse.agent.tools as tools_mod

    order: list[tuple[str, str]] = []
    spawn_counter = {"n": 0}

    async def fake_iframe_targets(session):
        return []

    async def fake_spawn(session, url):
        spawn_counter["n"] += 1
        tid = f"tid-{spawn_counter['n']}"
        order.append(("spawn", tid))
        return tid

    async def fake_close(session, tid):
        order.append(("close", tid))

    async def fake_focus(session, tid):
        order.append(("focus", tid))

    monkeypatch.setattr(tools_mod, "_iframe_targets", fake_iframe_targets)
    monkeypatch.setattr(tools_mod, "_spawn_tab", fake_spawn)
    monkeypatch.setattr(tools_mod, "_close_spawned_tab", fake_close)
    monkeypatch.setattr(tools_mod, "_focus_target", fake_focus)
    monkeypatch.setattr(tools_mod, "_read_one_page", read_one)
    session = types.SimpleNamespace(agent_focus_target_id=home)
    return tools_mod, order, session


async def test_read_pages_impl_focuses_home_before_closing(monkeypatch) -> None:
    async def read_one(
        session, url, tid, url_contains, claimed, baseline, allow_sole_candidate=False,
        sibling_urls=None,
    ):
        return {"url": url, "text": "body " * 60, "jsonld": None, "links": []}

    tools_mod, order, session = _wave_fakes(monkeypatch, read_one)
    urls = ["https://x.com/1", "https://x.com/2"]
    await tools_mod._read_pages_impl(session, urls, None, {}, concurrency=2)

    first_close = order.index(("close", "tid-1"))
    home_focus_before = [
        i for i, ev in enumerate(order) if ev == ("focus", "home-tid") and i < first_close
    ]
    assert home_focus_before, f"no home focus before first close: {order}"
    assert ("close", "tid-2") in order


async def test_read_pages_impl_closes_tabs_even_when_cancelled(monkeypatch) -> None:
    import asyncio

    import pytest

    async def read_one(
        session, url, tid, url_contains, claimed, baseline, allow_sole_candidate=False,
        sibling_urls=None,
    ):
        if url.endswith("/2"):
            raise asyncio.CancelledError()
        return {"url": url, "text": "body " * 60, "jsonld": None, "links": []}

    tools_mod, order, session = _wave_fakes(monkeypatch, read_one)
    urls = ["https://x.com/1", "https://x.com/2"]
    with pytest.raises(asyncio.CancelledError):
        await tools_mod._read_pages_impl(session, urls, None, {}, concurrency=2)

    closes = {tid for ev, tid in order if ev == "close"}
    assert closes == {"tid-1", "tid-2"}, f"tabs orphaned: {order}"


async def test_read_pages_impl_budget_stops_before_starting(monkeypatch) -> None:
    async def read_one(
        session, url, tid, url_contains, claimed, baseline, allow_sole_candidate=False,
        sibling_urls=None,
    ):
        return {"url": url, "text": "body " * 60, "jsonld": None, "links": []}

    tools_mod, order, session = _wave_fakes(monkeypatch, read_one)
    monkeypatch.setattr(tools_mod, "_READ_PAGES_BUDGET_S", 0.0)
    urls = ["https://x.com/1", "https://x.com/2"]
    clipboard: dict = {}
    pages = await tools_mod._read_pages_impl(session, urls, None, clipboard, concurrency=2)

    assert all("not attempted" in (p.get("error") or "") for p in pages)
    assert not [ev for ev in order if ev[0] == "spawn"]
    assert {tools_mod._norm_url(u) for u in urls} <= clipboard["_read_failed_frame"]
    assert not clipboard["_read_failed"]


def test_draft_row_flattens_nested_jsonld_and_maps_links() -> None:
    from openbrowse.agent.tools import _draft_row

    store = _draft_store()
    page = {
        "url": "https://x.com/listings?id=abc12345",
        "title": "Widget One",
        "text": "t" * 500,
        "jsonld": {
            "@type": "Product",
            "title": "Widget One",
            "datePublished": "2026-08-04",
            "jobLocation": {
                "@type": "Place",
                "address": {"@type": "PostalAddress", "addressLocality": "London"},
            },
            "employmentType": "FULL_TIME",
        },
        "links": [
            {"text": "Learn more", "href": "https://x.com/about"},
            {"text": "Order this widget", "href": "https://x.com/orders/abc/order"},
        ],
    }
    schema_store = store
    row = _draft_row(schema_store, page)
    assert row["publishedAt"] == "2026-08-04"
    assert "condition" not in row
    extra_keys = {e["key"] for e in row.get("extra") or []}
    assert "employmentType" in extra_keys


def test_draft_row_maps_url_field_from_links() -> None:
    from openbrowse.agent.output_store import OutputStore
    from openbrowse.agent.schema import json_schema_to_pydantic
    from openbrowse.agent.tools import _draft_row

    schema = {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["title"],
                    "properties": {
                        "title": {"type": "string"},
                        "sourceUrl": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                        "applyUrl": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                    },
                },
            }
        },
    }
    store = OutputStore(json_schema_to_pydantic(schema, "T"))
    page = {
        "url": "https://x.com/job?id=abc12345",
        "title": "Role",
        "text": "t" * 500,
        "jsonld": None,
        "links": [
            {"text": "Overview", "href": "https://x.com/overview"},
            {"text": "Apply for this Job", "href": "https://x.com/apply/abc"},
        ],
    }
    row = _draft_row(store, page)
    assert row["applyUrl"] == "https://x.com/apply/abc"
    assert row["sourceUrl"] == "https://x.com/job?id=abc12345"


def test_strong_overlap_guards_weak_tokens() -> None:
    from openbrowse.agent.tools import _strong_overlap

    assert _strong_overlap({"posted", "at"}, {"date", "posted"}) is True
    assert _strong_overlap({"location", "type"}, {"employment", "type"}) is False
    assert _strong_overlap({"location", "type"}, {"location", "type"}) is True


def test_gate_settles_partial_fields_when_all_pages_read() -> None:
    from openbrowse.agent.tools import _gate_empty_fields

    store = _items_store()
    store.add_item({"title": "A", "sourceUrl": "https://x.com/a", "description": "d" * 300})
    store.add_item({"title": "B", "sourceUrl": "https://x.com/b"})

    clipboard = {"_visited": {"https://x.com/a", "https://x.com/b"}, "_read_failed": set()}
    entries = _gate_empty_fields(store, clipboard)
    assert not any(e.startswith("description") for e in entries)

    partial_coverage = {"_visited": {"https://x.com/a"}, "_read_failed": set()}
    entries = _gate_empty_fields(store, partial_coverage)
    assert any(e.startswith("description") for e in entries)


def test_mark_absent_rejected_for_partial_field_with_full_coverage() -> None:
    from openbrowse.agent.tools import _absence_unearned

    store = _items_store()
    store.add_item({"title": "A", "sourceUrl": "https://x.com/a", "description": "d" * 300})
    store.add_item({"title": "B", "sourceUrl": "https://x.com/b", "description": "d" * 300})
    store.update_item(1, {"description": None})
    clipboard = {"_visited": {"https://x.com/a", "https://x.com/b"}}
    msg = _absence_unearned(store, clipboard, "description")
    assert msg is not None and "already settled" in msg


async def test_sandbox_asyncio_run_works(tmp_path) -> None:
    import types

    from browser_use import Tools

    from openbrowse.agent.tools import register_code_tools

    class _DirFileSystem(_FakeFileSystem):
        def __init__(self, base) -> None:
            super().__init__()
            self._base = base

        def get_dir(self):
            return self._base

    tools = Tools()
    register_code_tools(tools, {})
    fs = _DirFileSystem(tmp_path)
    entry = tools.registry.registry.actions["run_code_file"]
    code = (
        "async def main():\n"
        "    return 41 + 1\n"
        "print(asyncio.run(main()))"
    )
    params = entry.param_model(name="t2", code=code)
    result = await entry.function(
        params=params, browser_session=types.SimpleNamespace(), file_system=fs
    )
    assert not result.error, result.error
    assert "42" in result.extracted_content


def test_load_saved_json_disk_fallback(tmp_path) -> None:
    from openbrowse.agent.tools import _load_saved_json

    class _DirFileSystem(_FakeFileSystem):
        def __init__(self, base) -> None:
            super().__init__()
            self._base = base

        def get_dir(self):
            return self._base

    fs = _DirFileSystem(tmp_path)
    (tmp_path / "onDisk.json").write_text('[{"index": 0, "fields": {}}]')
    data, fn = _load_saved_json(fs, "onDisk.json")
    assert data == [{"index": 0, "fields": {}}]
    missing, _ = _load_saved_json(fs, "nope.json")
    assert missing is None


async def test_mark_absent_action_accepts_field_list() -> None:
    from openbrowse.agent.tools import register_output_store_tools

    tools = Tools()
    store = _items_store()
    clipboard = {"_visited": {"https://x.com/a"}}
    store.add_item({"title": "A", "sourceUrl": "https://x.com/a"})
    register_output_store_tools(tools, store, clipboard)
    entry = tools.registry.registry.actions["mark_absent"]
    params = entry.param_model(field=["description"], reason="never published")
    result = await entry.function(params=params, file_system=_FakeFileSystem())
    assert not result.error, result.error
    assert "description" in store.absent_fields


async def test_add_items_from_file_lists_loaded_titles() -> None:
    import json as _json

    from openbrowse.agent.tools import register_output_store_tools

    tools = Tools()
    store = _items_store()
    clipboard = {"_visited": {"https://x.com/a", "https://x.com/b"}}
    register_output_store_tools(tools, store, clipboard)
    fs = _FakeFileSystem()
    fs.files["rows.json"] = _json.dumps(
        [
            {"title": "Alpha", "sourceUrl": "https://x.com/a", "description": "d" * 300},
            {"title": "Beta", "sourceUrl": "https://x.com/b", "description": "d" * 300},
        ]
    )
    entry = tools.registry.registry.actions["add_items_from_file"]
    params = entry.param_model(name="rows.json")
    result = await entry.function(params=params, file_system=fs)
    assert "#0 Alpha" in result.extracted_content
    assert "#1 Beta" in result.extracted_content


def test_find_links_offhost_flagging_helper() -> None:
    from collections import Counter
    from urllib.parse import urlparse

    links = [
        {"href": "https://www.x.com/a"},
        {"href": "https://x.com/b"},
        {"href": "https://x.com/c"},
        {"href": "https://www.other.com/brand"},
    ]
    hosts = [urlparse(link["href"]).netloc.lower().removeprefix("www.") for link in links]
    majority = Counter(h for h in hosts if h).most_common(1)[0][0]
    flagged = [link for link, h in zip(links, hosts) if h and h != majority]
    assert majority == "x.com"
    assert [f["href"] for f in flagged] == ["https://www.other.com/brand"]


def test_url_discriminators_extracts_long_tokens() -> None:
    from openbrowse.agent.tools import _url_discriminators

    tokens = _url_discriminators(
        "https://www.example.com/listings/category-x?item_id=3dee50f9-717b-4311&ref=ab"
    )
    assert "3dee50f9-717b-4311" in tokens
    assert "category-x" in tokens
    assert "ab" not in tokens
    assert _url_discriminators("") == set()


async def test_mark_absent_accepts_read_failures_as_looked() -> None:
    from openbrowse.agent.tools import _absence_unearned

    store = _items_store()
    clipboard: dict = {"_visited": {"https://x.com/a"}}
    store.add_item({"title": "A", "sourceUrl": "https://x.com/a"})
    store.add_item({"title": "B", "sourceUrl": "https://x.com/b"})

    assert _absence_unearned(store, clipboard, "description") is not None

    clipboard["_read_failed"] = {"https://x.com/b"}
    assert _absence_unearned(store, clipboard, "description") is None


async def test_read_one_page_waits_out_loading_shell_and_jsonld(monkeypatch) -> None:
    import json as _json

    import openbrowse.agent.tools as tools_mod

    texts = iter(["Loading", "Loading…", "X" * 300, "X" * 300, "X" * 400])
    jsonlds = iter([[], [_json.dumps({"@type": "JobPosting", "datePublished": "2026-08-04"})]])
    last_text = {"v": ""}

    async def fake_eval(session, tid, js):
        if js == tools_mod._BODY_TEXT_JS:
            last_text["v"] = next(texts, last_text["v"])
            return last_text["v"]
        if js == tools_mod._JSONLD_JS:
            return next(jsonlds, [])
        if js == tools_mod._LINKS_JS:
            return [{"text": "Apply", "href": "https://x.com/apply"}]
        if "readyState" in js:
            return "complete"
        return "Job title"

    async def fake_match(
        session, tid, needle, claimed, baseline, allow_sole=False, page_url=None,
        sibling_urls=None,
    ):
        return "frame-1"

    monkeypatch.setattr(tools_mod, "_eval_on_target", fake_eval)
    monkeypatch.setattr(tools_mod, "_match_frame_target", fake_match)

    page = await tools_mod._read_one_page(
        None, "https://x.com/j1", "tid-1", "embed", set(), set()
    )
    assert not page.get("error")
    assert len(page["text"]) >= 300
    assert page["jsonld"]["datePublished"] == "2026-08-04"
    assert page["frame_matched"] is True


async def test_mark_absent_requires_pages_read() -> None:
    from openbrowse.agent.tools import _absence_unearned

    store = _items_store()
    clipboard: dict = {"_visited": {"https://x.com/a", "https://x.com/b"}}
    store.add_item({"title": "A", "sourceUrl": "https://x.com/a"})
    store.add_item({"title": "B", "sourceUrl": "https://x.com/b"})

    blocked = _absence_unearned(store, {}, "description")
    assert blocked is not None and "read_pages" in blocked

    assert _absence_unearned(store, clipboard, "description") is None


async def test_mark_absent_earn_check_skips_top_level_and_urlless() -> None:
    from openbrowse.agent.output_store import OutputStore
    from openbrowse.agent.schema import json_schema_to_pydantic
    from openbrowse.agent.tools import _absence_unearned

    store = _items_store()
    assert _absence_unearned(store, {}, "indexPageUrl") is None
    assert _absence_unearned(store, {}, "description") is None

    schema = {
        "type": "object",
        "properties": {
            "rows": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"name": {"anyOf": [{"type": "string"}, {"type": "null"}]}},
                },
            }
        },
    }
    urlless = OutputStore(json_schema_to_pydantic(schema, "T"))
    urlless.add_item({"name": "x"})
    assert _absence_unearned(urlless, {}, "name") is None


def _hints_store():
    from openbrowse.agent.output_store import OutputStore
    from openbrowse.agent.schema import json_schema_to_pydantic

    schema = {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["title"],
                    "properties": {
                        "title": {"type": "string"},
                        "publishedAt": {"anyOf": [{"type": "string"}, {"type": "null"}]},
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
                    },
                },
            }
        },
    }
    return OutputStore(json_schema_to_pydantic(schema, "T"))


async def test_completeness_gate_bounces_once_with_hints() -> None:
    from openbrowse.agent.tools import register_completeness_gate

    tools = Tools()
    store = _hints_store()
    store.add_item(
        {"title": "A", "extra": [{"key": "datePublished", "value": "2026-08-04"}]}
    )
    bounces: list[list[str]] = []

    async def on_incomplete(empties: list[str]) -> None:
        bounces.append(empties)

    register_completeness_gate(tools, store, on_incomplete)
    entry = tools.registry.registry.actions["done"]
    params = entry.param_model(text="all done", success=True)
    fs = _FakeFileSystem()

    first = await entry.function(params=params, file_system=fs)
    assert first.is_done is False
    assert "mark_absent" in first.extracted_content
    assert "datePublished" in first.extracted_content
    assert "publishedAt" in first.extracted_content
    assert len(bounces) == 1

    second = await entry.function(params=params, file_system=fs)
    assert second.is_done is True
    assert len(bounces) == 1


async def test_completeness_gate_passes_when_absent_marked() -> None:
    from openbrowse.agent.tools import register_completeness_gate

    tools = Tools()
    store = _hints_store()
    store.add_item({"title": "A"})
    store.mark_absent("publishedAt", "no dates published")
    store.mark_absent("extra", "no extra attributes shown")
    register_completeness_gate(tools, store, None)
    entry = tools.registry.registry.actions["done"]
    params = entry.param_model(text="all done", success=True)

    result = await entry.function(params=params, file_system=_FakeFileSystem())
    assert result.is_done is True


def test_draft_row_ranked_candidates_survive_enum_rejection() -> None:
    from openbrowse.agent.output_store import OutputStore
    from openbrowse.agent.schema import json_schema_to_pydantic
    from openbrowse.agent.tools import _draft_row

    schema = {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["title"],
                    "properties": {
                        "title": {"type": "string"},
                        "sourceUrl": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                        "locationType": {
                            "anyOf": [
                                {"type": "string", "enum": ["ONSITE", "HYBRID", "REMOTE"]},
                                {"type": "null"},
                            ]
                        },
                        "location": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                        "applyUrl": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                    },
                },
            }
        },
    }
    store = OutputStore(json_schema_to_pydantic(schema))
    page = {
        "url": "https://x.com/jobs?id=1",
        "title": "Role One",
        "text": "t" * 500,
        "jsonld": {
            "@type": "JobPosting",
            "title": "Role One",
            "directApply": True,
            "jobLocation": {
                "@type": "Place",
                "address": {"@type": "PostalAddress", "addressLocality": "London"},
            },
        },
    }
    row = _draft_row(store, page)
    assert row["location"] == "London"
    assert "locationType" not in row
    assert row.get("applyUrl") != "true"
    assert "applyUrl" not in row


class _DirFs:
    def __init__(self, base) -> None:
        self._base = base

    def get_dir(self):
        return self._base

    def get_file(self, name):
        return None

    async def write_file(self, name, content):
        (self._base / name).write_text(content)


async def _run_sandbox(tmp_path, code, session=None, monkeypatch=None):
    import types

    from openbrowse.agent.tools import register_code_tools

    tools = Tools()
    register_code_tools(tools, {}, None)
    entry = tools.registry.registry.actions["run_code_file"]
    params = entry.param_model(name="t", code=code)
    return await entry.function(
        params=params,
        browser_session=session or types.SimpleNamespace(),
        file_system=_DirFs(tmp_path),
    )


async def test_run_code_file_reports_saved_files(tmp_path) -> None:
    result = await _run_sandbox(tmp_path, "save_json({'k': 1}, 'made.json')\nprint('ok')")
    assert not result.error, result.error
    assert "Files saved this run: made.json." in result.extracted_content

    result = await _run_sandbox(tmp_path, "print('nothing to save')")
    assert "No files were saved by this script" in result.extracted_content


async def test_run_code_file_hints_on_string_indices(tmp_path) -> None:
    """The hint must not travel inside ``error``: browser-use renders an error over
    200 chars as its first 100 plus last 100, so a hint sitting between the exception
    and stdout is deleted before the model reads it."""
    from openbrowse.agent.tools import model_visible_attrs

    result = await _run_sandbox(tmp_path, "x = '{\"a\": 1}'\nprint(x['a'])")
    assert result.error
    assert "TypeError" in result.error
    assert "parsed dicts" not in result.error

    visible = "\n".join(str(getattr(result, a) or "") for a in model_visible_attrs(result))
    assert "parsed dicts" in visible
    assert "read_output()" in visible


async def test_run_code_file_shows_code_tab_and_restores_focus(
    tmp_path, monkeypatch
) -> None:
    import types

    import openbrowse.agent.tools as tools_mod

    order: list[tuple[str, str]] = []

    async def fake_spawn(session, url):
        assert url.endswith("/codeview")
        order.append(("spawn", "code-tid"))
        return "code-tid"

    async def fake_focus(session, tid):
        order.append(("focus", tid))

    async def fake_close(session, tid):
        order.append(("close", tid))

    monkeypatch.setattr(tools_mod, "_spawn_tab", fake_spawn)
    monkeypatch.setattr(tools_mod, "_focus_target", fake_focus)
    monkeypatch.setattr(tools_mod, "_close_spawned_tab", fake_close)

    session = types.SimpleNamespace(agent_focus_target_id="home-tid")
    result = await _run_sandbox(tmp_path, "print('hi')", session=session)
    assert not result.error, result.error
    assert order == [
        ("spawn", "code-tid"),
        ("focus", "code-tid"),
        ("focus", "home-tid"),
        ("close", "code-tid"),
    ]


async def test_run_code_file_emits_writing_and_running_events(tmp_path) -> None:
    from openbrowse.agent.tools import register_code_tools

    events: list[str] = []

    async def progress(msg: str) -> None:
        events.append(msg)

    tools = Tools()
    register_code_tools(tools, {}, None, progress)
    entry = tools.registry.registry.actions["run_code_file"]
    params = entry.param_model(name="calc", code="x = 1\nprint(x)")
    import types as _types

    result = await entry.function(
        params=params,
        browser_session=_types.SimpleNamespace(),
        file_system=_DirFs(tmp_path),
    )
    assert not result.error, result.error
    assert events == ["▶ Running calc.py"]


def test_partial_json_string_prefix_handles_escapes() -> None:
    from openbrowse.agent.code_stream import _partial_json_string_prefix

    assert _partial_json_string_prefix('x = 1\\nprint(x)", "rest') == "x = 1\nprint(x)"
    assert _partial_json_string_prefix("half an esc\\") == "half an esc"
    assert _partial_json_string_prefix('unicode \\u00a3 sign') == "unicode £ sign"
    assert _partial_json_string_prefix('trunc \\u00') == "trunc "


async def test_code_stream_observer_announces_and_pushes(monkeypatch) -> None:
    import openbrowse.agent.tools as tools_mod
    from openbrowse.agent.code_stream import CodeStreamObserver

    spawned: list[str] = []
    focused: list[str] = []

    async def fake_spawn(session, url):
        spawned.append(url)
        return "code-tid"

    async def fake_focus(session, tid):
        focused.append(tid)

    import openbrowse.agent.code_stream as cs_mod

    monkeypatch.setattr(tools_mod, "_spawn_tab", fake_spawn)
    monkeypatch.setattr(tools_mod, "_focus_target", fake_focus)
    monkeypatch.setattr(cs_mod, "_PUSH_INTERVAL_S", 0.0)

    events: list[str] = []

    async def progress(msg: str) -> None:
        events.append(msg)

    pushes: list[tuple] = []

    clipboard: dict = {}
    obs = CodeStreamObserver(object(), clipboard, progress)

    async def fake_push(name, code, status, target=None):
        pushes.append((name, code, status))

    obs.push = fake_push

    await obs.on_partial('{"thinking": "let me browse this page"')
    assert events == [] and spawned == []

    await obs.on_partial(
        '{"thinking": "…", "action": [{"run_code_file": {"name": "calc.py", "code": "x = 1\\n'
    )
    assert events == ["⌨️ Writing calc.py"]
    assert spawned and spawned[0].endswith("/codeview")
    assert focused == ["code-tid"]
    assert clipboard["_code_stream_tab"] == "code-tid"
    assert clipboard["_code_stream"] is obs

    await obs.on_partial(
        '{"thinking": "…", "action": [{"run_code_file": {"name": "calc.py", "code": "x = 1\\nprint(x)\\n'
    )
    assert pushes and pushes[-1][2] == "Writing"
    assert pushes[-1][1].startswith("x = 1\nprint(x)")

    obs.reset()
    assert "_code_stream_tab" not in clipboard


async def test_code_stream_ignores_prose_mentions(monkeypatch) -> None:
    import openbrowse.agent.tools as tools_mod
    from openbrowse.agent.code_stream import CodeStreamObserver

    async def fake_spawn(session, url):
        raise AssertionError("must not open a tab for prose mentions")

    monkeypatch.setattr(tools_mod, "_spawn_tab", fake_spawn)
    obs = CodeStreamObserver(object(), {}, None)
    await obs.on_partial(
        '{"thinking": "I could use run_code_file here, or maybe update_items instead"'
    )
    assert obs._announced is False


async def test_code_stream_settle_restores_focus_and_closes_orphans(monkeypatch) -> None:
    import openbrowse.agent.tools as tools_mod
    from openbrowse.agent.code_stream import CodeStreamObserver

    focused: list[str] = []
    closed: list[str] = []

    async def fake_spawn(session, url):
        return "code-tid"

    async def fake_focus(session, tid):
        focused.append(tid)

    async def fake_close(session, tid):
        closed.append(tid)

    monkeypatch.setattr(tools_mod, "_spawn_tab", fake_spawn)
    monkeypatch.setattr(tools_mod, "_focus_target", fake_focus)
    monkeypatch.setattr(tools_mod, "_close_spawned_tab", fake_close)

    clipboard: dict = {}
    session = __import__("types").SimpleNamespace(agent_focus_target_id="page-tid")
    obs = CodeStreamObserver(session, clipboard, None)
    await obs.on_partial('{"action": [{"run_code_file": {"name": "calc.py", "code": "x')
    assert clipboard["_code_stream_tab"] == "code-tid"

    await obs.settle(has_run_code_file=True)
    assert focused[-1] == "page-tid"
    assert closed == []
    assert clipboard["_code_stream_tab"] == "code-tid"

    await obs.settle(has_run_code_file=False)
    assert closed == ["code-tid"]
    assert "_code_stream_tab" not in clipboard


def test_compact_json_text_elides_long_strings() -> None:
    import json as _json

    from openbrowse.agent.tools import _compact_json_text

    long_text = "x" * 500
    text = "Read from file pages.json:\n" + _json.dumps(
        [{"url": "https://x.com/a", "text": long_text}]
    )
    out = _compact_json_text(text)
    assert out is not None
    assert "<500 chars>" in out
    assert "https://x.com/a" in out
    assert out.startswith("Read from file pages.json:")
    assert "1 long value(s) elided" in out

    assert _compact_json_text("plain prose with no json at all " * 40) is None
    assert _compact_json_text(_json.dumps({"a": "short"})) is None


async def test_output_guard_compacts_oversized_json_instead_of_truncating() -> None:
    import json as _json

    from browser_use import ActionResult

    from openbrowse.agent.tools import register_output_guard_overrides

    tools = Tools()
    big_json = _json.dumps([{"i": i, "text": "y" * 600} for i in range(30)])
    big_prose = "z" * 20_000

    @tools.action("fake dump")
    async def run_code_file(code: str) -> ActionResult:
        return ActionResult(extracted_content=big_json)

    @tools.action("fake prose dump")
    async def evaluate(js: str) -> ActionResult:
        return ActionResult(extracted_content=big_prose)

    register_output_guard_overrides(tools)

    entry = tools.registry.registry.actions["run_code_file"]
    result = await entry.function(params=entry.param_model(code="x"))
    assert "<600 chars>" in result.extracted_content
    assert "30 long value(s) elided" in result.extracted_content
    assert "truncated" not in result.extracted_content
    assert len(result.extracted_content) < len(big_json)

    entry = tools.registry.registry.actions["evaluate"]
    result = await entry.function(params=entry.param_model(js="x"))
    assert "[truncated:" in result.extracted_content


def test_saved_links_skip_offhost_for_no_args_reads() -> None:
    from openbrowse.agent.tools import _saved_links_sans_offhost

    clipboard = {
        "found_links": ["https://x.com/a", "https://other.com/brand", "https://x.com/b"],
        "found_links_offhost": {"https://other.com/brand"},
    }
    kept, skipped = _saved_links_sans_offhost(clipboard)
    assert kept == ["https://x.com/a", "https://x.com/b"]
    assert skipped == 1

    kept, skipped = _saved_links_sans_offhost({"found_links": ["https://x.com/a"]})
    assert kept == ["https://x.com/a"] and skipped == 0
    assert _saved_links_sans_offhost(None) == ([], 0)


async def test_read_pages_default_wave_size_is_six(monkeypatch) -> None:
    events: list[str] = []

    async def progress(msg: str) -> None:
        events.append(msg)

    async def read_one(
        session, url, tid, url_contains, claimed, baseline, allow_sole_candidate=False,
        sibling_urls=None,
    ):
        return {"url": url, "text": "t" * 300, "jsonld": None, "links": []}

    tools_mod, order, session = _wave_fakes(monkeypatch, read_one)
    urls = [f"https://x.com/{i}" for i in range(16)]
    await tools_mod._read_pages_impl(session, urls, None, {}, progress=progress)
    assert any("3 wave(s) of up to 6 tabs" in e for e in events)
    assert any("(" in e and "s)" in e for e in events if "wave 1/3" in e)


def test_mark_absent_allowed_after_source_url_rewrite() -> None:
    from openbrowse.agent.tools import _absence_unearned, _refresh_read_items

    store = _items_store()
    store.add_item({"title": "A", "sourceUrl": "https://x.com/a"})
    store.add_item({"title": "B", "sourceUrl": "https://x.com/b"})
    clipboard = {"_visited": {"https://x.com/a", "https://x.com/b"}, "_read_failed": set()}

    read = _refresh_read_items(store, clipboard)
    assert read == {0, 1}

    store.update_item(0, {"sourceUrl": "https://ats.example.com/a"})
    store.update_item(1, {"sourceUrl": "https://ats.example.com/b"})

    assert _absence_unearned(store, clipboard, "description") is None


def test_gate_settles_partial_field_after_source_url_rewrite() -> None:
    from openbrowse.agent.tools import _gate_empty_fields, _refresh_read_items

    store = _items_store()
    store.add_item({"title": "A", "sourceUrl": "https://x.com/a", "description": "d" * 300})
    store.add_item({"title": "B", "sourceUrl": "https://x.com/b"})
    clipboard = {"_visited": {"https://x.com/a", "https://x.com/b"}, "_read_failed": set()}
    _refresh_read_items(store, clipboard)

    store.update_item(0, {"sourceUrl": "https://ats.example.com/a"})
    store.update_item(1, {"sourceUrl": "https://ats.example.com/b"})

    entries = _gate_empty_fields(store, clipboard)
    assert not any(e.startswith("description") for e in entries)


def test_unread_items_still_block_absence_after_rewrite_of_others() -> None:
    from openbrowse.agent.tools import _absence_unearned, _refresh_read_items

    store = _items_store()
    store.add_item({"title": "A", "sourceUrl": "https://x.com/a"})
    store.add_item({"title": "B", "sourceUrl": "https://x.com/never-read"})
    clipboard = {"_visited": {"https://x.com/a"}, "_read_failed": set()}
    _refresh_read_items(store, clipboard)
    store.update_item(0, {"sourceUrl": "https://ats.example.com/a"})
    msg = _absence_unearned(store, clipboard, "description")
    assert msg is not None and "have not been read" in msg


async def test_read_one_page_skips_frame_filter_on_panel_host(monkeypatch) -> None:
    import openbrowse.agent.tools as tools_mod

    matched = {"called": False}

    async def fake_match(*args, **kwargs):
        matched["called"] = True
        return None

    async def fake_eval(session, tid, js):
        if "readyState" in js:
            return "complete"
        if js is tools_mod._BODY_TEXT_JS or "innerText" in str(js):
            return "role detail " * 50
        return []

    monkeypatch.setattr(tools_mod, "_match_frame_target", fake_match)
    monkeypatch.setattr(tools_mod, "_eval_on_target", fake_eval)
    page = await tools_mod._read_one_page(
        object(),
        "https://board.example.com/acme/item-1",
        "tid-main",
        "board.example.com",
        set(),
        set(),
    )
    assert not page.get("error")
    assert matched["called"] is False
    assert not page.get("frame_matched")
    assert page.get("frame_skipped_own_host") is True


async def test_read_one_page_falls_back_to_main_doc_when_no_frame(monkeypatch) -> None:
    import openbrowse.agent.tools as tools_mod

    async def fake_match(*args, **kwargs):
        return None

    async def fake_eval(session, tid, js):
        if "readyState" in js:
            return "complete"
        if js is tools_mod._BODY_TEXT_JS or "innerText" in str(js):
            return "plain page content " * 50
        return []

    monkeypatch.setattr(tools_mod, "_match_frame_target", fake_match)
    monkeypatch.setattr(tools_mod, "_eval_on_target", fake_eval)
    monkeypatch.setattr(tools_mod, "_FRAME_MATCH_GRACE_S", 0.0)
    page = await tools_mod._read_one_page(
        object(),
        "https://elsewhere.example.com/role-1",
        "tid-main",
        "board.example.com",
        set(),
        set(),
    )
    assert not page.get("error")
    assert not page.get("frame_matched")
    assert "plain page content" in page.get("text", "")


async def test_read_one_page_still_fails_on_hollow_main_doc(monkeypatch) -> None:
    import openbrowse.agent.tools as tools_mod

    async def fake_match(*args, **kwargs):
        return None

    async def fake_eval(session, tid, js):
        if "readyState" in js:
            return "complete"
        return ""

    monkeypatch.setattr(tools_mod, "_match_frame_target", fake_match)
    monkeypatch.setattr(tools_mod, "_eval_on_target", fake_eval)
    monkeypatch.setattr(tools_mod, "_FRAME_MATCH_GRACE_S", 0.0)
    monkeypatch.setattr(tools_mod, "_PAGE_READY_TIMEOUT_S", 0.6)
    page = await tools_mod._read_one_page(
        object(),
        "https://elsewhere.example.com/role-1",
        "tid-main",
        "board.example.com",
        set(),
        set(),
    )
    assert page.get("error")


def _apply_store():
    from openbrowse.agent.output_store import OutputStore
    from openbrowse.agent.schema import json_schema_to_pydantic

    schema = {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["title"],
                    "properties": {
                        "title": {"type": "string"},
                        "sourceUrl": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                        "applyUrl": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                        "description": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                    },
                },
            }
        },
    }
    return OutputStore(json_schema_to_pydantic(schema, "ApplyT"))


def test_draft_row_rejects_footer_text_and_fills_apply_url_from_links() -> None:
    from openbrowse.agent.tools import _draft_row

    store = _apply_store()
    page = {
        "url": "https://x.com/list/item-1",
        "title": "Senior Widget Fitter",
        "text": "Senior Widget Fitter\nApply\nPowered by\nBoardVendor\n" + "About the position " * 60,
        "jsonld": None,
        "links": [
            {"text": "Home", "href": "https://x.com/"},
            {
                "text": "Apply for this job",
                "href": "https://board.example.com/m/item-1/application",
            },
            {"text": "Powered by", "href": "javascript:void(0)"},
        ],
    }
    row = _draft_row(store, page)
    assert row.get("applyUrl") == "https://board.example.com/m/item-1/application"


def test_draft_row_leaves_apply_url_null_when_no_matching_link() -> None:
    from openbrowse.agent.tools import _draft_row

    store = _apply_store()
    page = {
        "url": "https://x.com/list/item-1",
        "title": "Role",
        "text": "Item\nApply\nPowered by\nBoardVendor\n" + "About the position " * 60,
        "jsonld": None,
        "links": [{"text": "Home", "href": "https://x.com/"}],
    }
    row = _draft_row(store, page)
    assert row.get("applyUrl") in (None, "")


async def test_done_text_carries_store_output_for_judge() -> None:
    from openbrowse.agent.tools import register_completeness_gate

    tools = Tools()
    store = _items_store()
    store.add_item(
        {"title": "A", "sourceUrl": "https://x.com/a", "description": "d" * 300}
    )
    store.mark_absent("description", "checked")
    register_completeness_gate(tools, store, None)
    entry = tools.registry.registry.actions["done"]
    params = entry.param_model(text="all done", success=True)
    fs = _FakeFileSystem()
    result = await entry.function(params=params, file_system=fs)
    assert result.is_done is True
    assert "FINAL STRUCTURED OUTPUT" in params.text
    assert '"title": "A"' in params.text
    assert params.text.rstrip().endswith("all done")


async def test_done_text_untouched_when_store_empty() -> None:
    from openbrowse.agent.tools import register_completeness_gate

    tools = Tools()
    store = _items_store()
    register_completeness_gate(tools, store, None)
    entry = tools.registry.registry.actions["done"]
    params = entry.param_model(text="all done", success=True)
    fs = _FakeFileSystem()
    first = await entry.function(params=params, file_system=fs)
    assert first.is_done is False
    second = await entry.function(params=params, file_system=fs)
    assert second.is_done is True
    assert params.text == "all done"


async def test_judge_injection_elides_long_values_keeping_all_records() -> None:
    from openbrowse.agent.tools import register_completeness_gate

    tools = Tools()
    store = _items_store()
    for i in range(16):
        store.add_item(
            {
                "title": f"Item {i}",
                "sourceUrl": f"https://x.com/{i}",
                "description": "long text " * 200,
            }
        )
    register_completeness_gate(tools, store, None)
    entry = tools.registry.registry.actions["done"]
    params = entry.param_model(text="all done", success=True)
    fs = _FakeFileSystem()
    result = await entry.function(params=params, file_system=fs)
    assert result.is_done is True
    assert "FINAL STRUCTURED OUTPUT" in params.text
    assert "long values elided" in params.text
    for i in range(16):
        assert f"Item {i}" in params.text, i


def _link_node(index, href, tag="a", target="main", text="link", parent=None):
    import types as _t

    return _t.SimpleNamespace(
        tag_name=tag,
        attributes={"href": href} if href else {},
        is_visible=True,
        target_id=target,
        content_document=None,
        parent_node=parent,
        session_id="s1",
        backend_node_id=index,
        get_meaningful_text_for_llm=lambda text=text: text,
    )


def _embed_map(with_frame_doc):
    import types as _t

    iframe = _t.SimpleNamespace(
        tag_name="iframe",
        attributes={"src": "https://board.example.com/acme/embed"},
        is_visible=True,
        target_id="frame-1",
        content_document=(
            _t.SimpleNamespace(target_id="frame-1") if with_frame_doc else None
        ),
        parent_node=None,
        session_id="s1",
        backend_node_id=1,
        get_meaningful_text_for_llm=lambda: "",
    )
    role1 = _link_node(11, "https://x.com/list?embed_jid=aaa", target="frame-1")
    role2 = _link_node(12, "https://x.com/list?embed_jid=bbb", target="frame-1")
    vendor = _link_node(30, "https://board.example.com/", target="main", text="Powered by")
    return {1: iframe, 11: role1, 12: role2, 30: vendor}


def test_scan_link_map_frame_filter_counts_matched_frames() -> None:
    from openbrowse.agent.tools import _scan_link_map

    links, frames, anchors, iframe_present = _scan_link_map(
        _embed_map(with_frame_doc=True),
        "https://x.com/list",
        frame_url_contains="board.example.com",
    )
    assert frames == 1
    assert iframe_present is True
    assert [l["href"] for l in links] == [
        "https://x.com/list?embed_jid=aaa",
        "https://x.com/list?embed_jid=bbb",
    ]


def test_scan_link_map_reports_zero_frames_instead_of_silent_empty() -> None:
    from openbrowse.agent.tools import _scan_link_map

    links, frames, anchors, iframe_present = _scan_link_map(
        _embed_map(with_frame_doc=False),
        "https://x.com/list",
        frame_url_contains="board.example.com",
    )
    assert frames == 0
    assert links == []
    assert anchors == 3
    assert iframe_present is True


async def test_find_links_retries_then_errors_honestly_when_frame_missing(
    monkeypatch,
) -> None:

    import openbrowse.agent.tools as tools_mod
    from browser_use import Tools

    monkeypatch.setattr(tools_mod, "_FIND_LINKS_RETRY_DELAY_S", 0.0)

    scans = {"n": 0}
    selector_map = _embed_map(with_frame_doc=False)

    async def fake_settle(session, frame):
        return None

    async def fake_eval(session, js):
        return "https://x.com/list"

    monkeypatch.setattr(tools_mod, "_settle_lazy_links", fake_settle)
    monkeypatch.setattr(tools_mod, "_eval_js", fake_eval)

    class FakeSession:
        async def get_browser_state_summary(self, include_screenshot=False):
            scans["n"] += 1

        async def get_selector_map(self):
            return selector_map

        async def get_element_by_index(self, index):
            return None

    progress_msgs = []

    async def progress(label):
        progress_msgs.append(label)

    tools = Tools()
    clipboard: dict = {}
    tools_mod.register_tab_tools(
        tools, object(), clipboard, None, progress
    )
    entry = tools.registry.registry.actions["find_links"]
    result = await entry.function(
        browser_session=FakeSession(),
        file_system=_FakeFileSystem(),
        frame_url_contains="board.example.com",
    )
    assert scans["n"] == 1 + tools_mod._FIND_LINKS_MAX_RETRIES
    assert result.error and "No embedded frame matching" in result.error
    assert "open them by index" in result.error
    assert any("matched 0 frame(s)" in m for m in progress_msgs)


async def test_find_links_frameless_retry_when_embed_present(monkeypatch) -> None:
    import openbrowse.agent.tools as tools_mod
    from browser_use import Tools

    monkeypatch.setattr(tools_mod, "_FIND_LINKS_RETRY_DELAY_S", 0.0)
    first_map = _embed_map(with_frame_doc=True)
    late_map = dict(first_map)
    late_map[13] = _link_node(13, "https://x.com/list?embed_jid=ccc", target="frame-1")
    late_map[14] = _link_node(14, "https://x.com/list?embed_jid=ddd", target="frame-1")
    maps = [
        {1: first_map[1], 30: first_map[30]},
        late_map,
    ]

    async def fake_settle(session, frame):
        return None

    async def fake_eval(session, js):
        return "https://x.com/list"

    monkeypatch.setattr(tools_mod, "_settle_lazy_links", fake_settle)
    monkeypatch.setattr(tools_mod, "_eval_js", fake_eval)

    class FakeSession:
        async def get_browser_state_summary(self, include_screenshot=False):
            return None

        async def get_selector_map(self):
            return maps.pop(0) if len(maps) > 1 else maps[0]

        async def get_element_by_index(self, index):
            return None

    tools = Tools()
    tools_mod.register_tab_tools(tools, object(), {}, None, None)
    entry = tools.registry.registry.actions["find_links"]
    result = await entry.function(
        browser_session=FakeSession(),
        file_system=_FakeFileSystem(),
        href_contains="embed_jid",
    )
    assert not result.error
    import json as _json

    found = _json.loads(result.extracted_content)["data"]
    assert len(found) == 4
    assert all("embed_jid" in l["href"] for l in found)


async def test_flag_shell_reads_falls_back_to_dom_probe(monkeypatch) -> None:
    import openbrowse.agent.tools as tools_mod

    async def no_targets(session):
        return []

    async def dom_hosts(session):
        return ["embed.example.com"]

    monkeypatch.setattr(tools_mod, "_iframe_targets", no_targets)
    monkeypatch.setattr(tools_mod, "_dom_iframe_hosts", dom_hosts)

    results = {
        f"https://x.com/{i}": {"url": f"https://x.com/{i}", "text": "Acme careers portal welcome"}
        for i in range(3)
    }
    flagged, hosts = await tools_mod._flag_shell_reads(None, results)
    assert flagged == 3
    assert hosts == ["embed.example.com"]
    assert all("embedding shell" in p["error"] for p in results.values())


async def test_flag_shell_reads_tolerates_digit_noise(monkeypatch) -> None:
    import openbrowse.agent.tools as tools_mod

    async def no_targets(session):
        return []

    async def dom_hosts(session):
        return ["embed.example.com"]

    monkeypatch.setattr(tools_mod, "_iframe_targets", no_targets)
    monkeypatch.setattr(tools_mod, "_dom_iframe_hosts", dom_hosts)

    results = {
        f"https://x.com/{i}": {
            "url": f"https://x.com/{i}",
            "text": f"Acme careers portal  {i * 17} open roles today {i}",
        }
        for i in range(3)
    }
    flagged, _hosts = await tools_mod._flag_shell_reads(None, results)
    assert flagged == 3


async def test_gate_bounces_on_link_deficit_then_passes() -> None:
    from openbrowse.agent.tools import register_completeness_gate
    from browser_use import Tools

    tools = Tools()
    store = _items_store()
    store.add_item(
        {"title": "A", "sourceUrl": "https://x.com/a", "description": "d" * 200}
    )
    store.add_item(
        {"title": "B", "sourceUrl": "https://x.com/b", "description": "d" * 200}
    )
    clipboard: dict = {
        "found_links": [
            "https://x.com/a",
            "https://x.com/b",
            "https://x.com/c",
            "https://x.com/d",
            "https://other.com/nav",
        ],
        "found_links_offhost": {"https://other.com/nav"},
        "_visited": {"https://x.com/a", "https://x.com/b"},
    }
    register_completeness_gate(tools, store, None, clipboard)
    entry = tools.registry.registry.actions["done"]
    params = entry.param_model(text="all done", success=True)
    fs = _FakeFileSystem()

    first = await entry.function(params=params, file_system=fs)
    assert first.is_done is False
    assert "4 on-site link(s)" in first.extracted_content
    assert "only 2 item(s)" in first.extracted_content
    assert "https://x.com/c" in first.extracted_content
    assert "https://other.com/nav" not in first.extracted_content

    second = await entry.function(params=params, file_system=fs)
    assert second.is_done is True


async def test_gate_bounce_names_dom_embeds_when_no_links_found() -> None:
    from openbrowse.agent.tools import register_completeness_gate
    from browser_use import Tools

    tools = Tools()
    store = _items_store()
    store.add_item({"title": "A"})
    clipboard: dict = {"_dom_embed_hosts": ["board.example.com"]}
    register_completeness_gate(tools, store, None, clipboard)
    entry = tools.registry.registry.actions["done"]
    params = entry.param_model(text="all done", success=True)

    first = await entry.function(params=params, file_system=_FakeFileSystem())
    assert first.is_done is False
    assert "never captured any links" in first.extracted_content
    assert "board.example.com" in first.extracted_content


def test_frame_failure_classifier() -> None:
    from openbrowse.agent.tools import _frame_failure

    assert _frame_failure("read the embedding shell, not this page's real content")
    assert _frame_failure("no embedded panel matching 'embed' rendered")
    assert _frame_failure("page embeds its content in a panel from x.com")
    assert _frame_failure("not attempted — read_pages stopped before its time budget")
    assert not _frame_failure("HTTPError: 404")
    assert not _frame_failure("no readable text rendered")


async def test_frame_failures_do_not_unlock_mark_absent() -> None:
    from openbrowse.agent.tools import _absence_unearned

    store = _items_store()
    clipboard: dict = {"_visited": {"https://x.com/a"}}
    store.add_item({"title": "A", "sourceUrl": "https://x.com/a"})
    store.add_item({"title": "B", "sourceUrl": "https://x.com/b"})

    clipboard["_read_failed_frame"] = {"https://x.com/b"}
    refusal = _absence_unearned(store, clipboard, "description")
    assert refusal is not None and "https://x.com/b" in refusal


def _sandbox_browser_with_frames(frames):
    from openbrowse.agent.tools import _SandboxBrowser

    sb = _SandboxBrowser(None)

    async def fake_frames():
        return frames

    sb.frames = fake_frames
    return sb


async def test_frame_evaluate_raises_instead_of_wrong_frame_fallback() -> None:
    import pytest

    sb = _sandbox_browser_with_frames(
        [{"targetId": "t1", "url": "https://consent.example.com/x"}]
    )
    with pytest.raises(RuntimeError) as err:
        await sb.frame_evaluate("board.example.com", "1+1")
    assert "consent.example.com" in str(err.value)

    sb_empty = _sandbox_browser_with_frames([])
    with pytest.raises(RuntimeError) as err2:
        await sb_empty.frame_evaluate("board.example.com", "1+1")
    assert "wait_for_frame" in str(err2.value)


async def test_wait_for_frame_returns_false_instead_of_raising() -> None:
    sb = _sandbox_browser_with_frames([])
    assert await sb.wait_for_frame("board.example.com", timeout_s=0.5) is False


async def test_navigate_raises_when_wait_for_frame_never_renders(monkeypatch) -> None:
    import pytest

    import openbrowse.agent.tools as tools_mod

    async def fake_eval(session, js):
        return None

    monkeypatch.setattr(tools_mod, "_eval_js", fake_eval)
    sb = _sandbox_browser_with_frames(
        [{"targetId": "t1", "url": "https://consent.example.com/x"}]
    )

    async def never(url_contains, timeout_s=12.0):
        return False

    sb.wait_for_frame = never
    with pytest.raises(RuntimeError) as err:
        await sb.navigate("https://x.com/j", wait_for="board.example.com")
    assert "board.example.com" in str(err.value)
    assert "consent.example.com" in str(err.value)


async def test_match_frame_target_rejects_shared_discriminator(monkeypatch) -> None:
    import types as _t

    import openbrowse.agent.tools as tools_mod

    targets = [
        {"targetId": "f1", "url": "https://panel.example.com/view/abcdefgh1234"}
    ]

    async def fake_targets(session):
        return targets

    async def fake_eval_on_target(session, tid, js):
        return []

    monkeypatch.setattr(tools_mod, "_iframe_targets", fake_targets)
    monkeypatch.setattr(tools_mod, "_eval_on_target", fake_eval_on_target)
    session = _t.SimpleNamespace()

    alone = await tools_mod._match_frame_target(
        session,
        "page-1",
        "panel",
        set(),
        set(),
        page_url="https://x.com/jobs?id=abcdefgh1234",
    )
    assert alone == "f1"

    shared = await tools_mod._match_frame_target(
        session,
        "page-1",
        "panel",
        set(),
        set(),
        page_url="https://x.com/jobs?id=abcdefgh1234",
        sibling_urls=[
            "https://x.com/jobs?id=abcdefgh1234",
            "https://x.com/other?id=abcdefgh1234",
        ],
    )
    assert shared is None


async def test_match_frame_target_sole_candidate_host_check(monkeypatch) -> None:
    import types as _t

    import openbrowse.agent.tools as tools_mod

    session = _t.SimpleNamespace()

    async def fake_eval_on_target(session_, tid, js):
        return []

    monkeypatch.setattr(tools_mod, "_eval_on_target", fake_eval_on_target)

    async def offhost_targets(session_):
        return [{"targetId": "f1", "url": "https://weird.example.com/ashby/x"}]

    monkeypatch.setattr(tools_mod, "_iframe_targets", offhost_targets)
    rejected = await tools_mod._match_frame_target(
        session,
        "page-1",
        "ashby",
        set(),
        set(),
        allow_sole_candidate=True,
        page_url="https://x.com/j",
    )
    assert rejected is None

    async def vendor_targets(session_):
        return [{"targetId": "f2", "url": "https://jobs.ashbyhq.com/acme"}]

    monkeypatch.setattr(tools_mod, "_iframe_targets", vendor_targets)
    accepted = await tools_mod._match_frame_target(
        session,
        "page-1",
        "ashby",
        set(),
        set(),
        allow_sole_candidate=True,
        page_url="https://x.com/j",
    )
    assert accepted == "f2"


async def test_settle_lazy_links_reports_never_matched_frame(monkeypatch) -> None:
    import openbrowse.agent.tools as tools_mod

    async def no_targets(session):
        return []

    async def fake_eval(session, js):
        return 0

    monkeypatch.setattr(tools_mod, "_iframe_targets", no_targets)
    monkeypatch.setattr(tools_mod, "_eval_js", fake_eval)
    monkeypatch.setattr(tools_mod, "_LAZY_POLL_S", 0.0)

    assert await tools_mod._settle_lazy_links(None, "board.example.com") is True
    assert await tools_mod._settle_lazy_links(None, None) is False


async def test_read_one_page_flags_short_main_doc_with_embeds(monkeypatch) -> None:
    import openbrowse.agent.tools as tools_mod

    async def fake_eval(session, tid, js):
        if js == tools_mod._BODY_TEXT_JS:
            return "X" * 250
        if js == tools_mod._JSONLD_JS:
            return []
        if js == tools_mod._LINKS_JS:
            return []
        if js == tools_mod._IFRAME_HOSTS_JS:
            return ["panel.example.com"]
        if "readyState" in js:
            return "complete"
        return "Title"

    monkeypatch.setattr(tools_mod, "_eval_on_target", fake_eval)
    monkeypatch.setattr(tools_mod, "_JSONLD_GRACE_S", 0.0)

    page = await tools_mod._read_one_page(
        None, "https://x.com/j1", "tid-1", None, set(), set()
    )
    assert "embeds its content in a panel from panel.example.com" in page["error"]
    assert tools_mod._frame_failure(page["error"])


async def test_find_links_notes_unverified_when_matched_frame_has_no_links(
    monkeypatch,
) -> None:
    import openbrowse.agent.tools as tools_mod
    from browser_use import Tools

    monkeypatch.setattr(tools_mod, "_FIND_LINKS_RETRY_DELAY_S", 0.0)

    async def fake_settle(session, frame):
        return False

    async def fake_eval(session, js):
        return "https://x.com/list"

    async def no_dom_hosts(session):
        return []

    monkeypatch.setattr(tools_mod, "_settle_lazy_links", fake_settle)
    monkeypatch.setattr(tools_mod, "_eval_js", fake_eval)
    monkeypatch.setattr(tools_mod, "_dom_iframe_hosts", no_dom_hosts)

    selector_map = _embed_map(with_frame_doc=True)
    for node in selector_map.values():
        if node.tag_name == "a":
            node.target_id = "main"

    class FakeSession:
        async def get_browser_state_summary(self, include_screenshot=False):
            return None

        async def get_selector_map(self):
            return selector_map

        async def get_element_by_index(self, index):
            return None

    tools = Tools()
    tools_mod.register_tab_tools(tools, object(), {}, None, None)
    entry = tools.registry.registry.actions["find_links"]
    result = await entry.function(
        browser_session=FakeSession(),
        file_system=_FakeFileSystem(),
        frame_url_contains="board.example.com",
    )
    assert not result.error
    assert "unverified" in result.long_term_memory
    assert "may not have finished rewriting" in result.long_term_memory


async def test_find_links_frameless_hint_from_dom_probe(monkeypatch) -> None:
    import openbrowse.agent.tools as tools_mod
    from browser_use import Tools

    monkeypatch.setattr(tools_mod, "_FIND_LINKS_RETRY_DELAY_S", 0.0)

    async def fake_settle(session, frame):
        return False

    async def fake_eval(session, js):
        return "https://x.com/list"

    async def dom_hosts(session):
        return ["board.example.com"]

    monkeypatch.setattr(tools_mod, "_settle_lazy_links", fake_settle)
    monkeypatch.setattr(tools_mod, "_eval_js", fake_eval)
    monkeypatch.setattr(tools_mod, "_dom_iframe_hosts", dom_hosts)

    selector_map = _embed_map(with_frame_doc=True)

    class FakeSession:
        async def get_browser_state_summary(self, include_screenshot=False):
            return None

        async def get_selector_map(self):
            return selector_map

        async def get_element_by_index(self, index):
            return None

    tools = Tools()
    clipboard: dict = {}
    tools_mod.register_tab_tools(tools, object(), clipboard, None, None)
    entry = tools.registry.registry.actions["find_links"]
    result = await entry.function(
        browser_session=FakeSession(),
        file_system=_FakeFileSystem(),
        href_contains="embed_jid",
    )
    assert not result.error
    assert "embeds cross-origin panel(s)" in result.long_term_memory
    assert "board.example.com" in result.long_term_memory
    assert clipboard["_dom_embed_hosts"] == ["board.example.com"]


async def test_read_pages_thin_pages_named_honestly(monkeypatch) -> None:
    import openbrowse.agent.tools as tools_mod
    from openbrowse.agent.tools import register_tab_tools
    from browser_use import Tools

    thin_url = "https://x.com/thin"

    async def fake_impl(session, urls, url_contains, clipboard, progress=None):
        return [
            {
                "url": "https://x.com/full",
                "title": "Full record",
                "text": "body " * 100,
                "jsonld": None,
                "links": [],
            },
            {
                "url": thin_url,
                "title": "Thin",
                "text": "almost nothing",
                "jsonld": None,
                "links": [],
            },
        ]

    monkeypatch.setattr(tools_mod, "_read_pages_impl", fake_impl)

    tools = Tools()
    store = _items_store()
    register_tab_tools(tools, object(), {}, store, None)
    entry = tools.registry.registry.actions["read_pages"]
    result = await entry.function(
        browser_session=object(),
        file_system=_FakeFileSystem(),
        urls=["https://x.com/full", thin_url],
    )
    assert not result.error
    assert "returned too little text to draft a row" in result.extracted_content
    assert "may have failed to render" in result.extracted_content
    assert thin_url in result.extracted_content
    assert "probably not records" not in result.extracted_content


async def test_open_in_new_tab_miss_names_unattached_embed(monkeypatch) -> None:
    import types as _t

    import openbrowse.agent.tools as tools_mod
    from openbrowse.agent.tools import TabManager

    async def none_element(index):
        return None

    session = _t.SimpleNamespace(
        get_element_by_index=lambda index: none_element(index)
    )

    async def dom_hosts(session_):
        return ["board.example.com"]

    monkeypatch.setattr(tools_mod, "_dom_iframe_hosts", dom_hosts)
    manager = TabManager(session)
    note = await manager.open_in_new_tab(7)
    assert "No element at index 7" in note
    assert "board.example.com" in note
    assert "frame_url_contains" in note


async def test_new_tabs_install_captcha_bridge_before_real_navigation(
    monkeypatch,
) -> None:
    import types as _t
    from unittest.mock import AsyncMock

    import openbrowse.agent.tools as tools_mod
    from openbrowse.agent.tools import TabManager

    class _Event:
        def __await__(self):
            async def done():
                return None

            return done().__await__()

        async def event_result(self, **kwargs):
            return None

    navigate = AsyncMock()
    cdp = _t.SimpleNamespace(
        cdp_client=_t.SimpleNamespace(
            send=_t.SimpleNamespace(Page=_t.SimpleNamespace(navigate=navigate))
        ),
        session_id="session-1",
    )
    session = _t.SimpleNamespace(
        _cdp_create_new_page=AsyncMock(return_value="target-1"),
        get_or_create_cdp_session=AsyncMock(return_value=cdp),
        event_bus=_t.SimpleNamespace(dispatch=lambda event: _Event()),
    )
    bridge = AsyncMock()
    monkeypatch.setattr(tools_mod, "install_captcha_bridge", bridge)

    target = await TabManager(session)._new_page(
        "https://challenge.example/page", background=True
    )

    assert target == "target-1"
    session._cdp_create_new_page.assert_awaited_once_with(
        "about:blank", background=True
    )
    bridge.assert_awaited_once_with(session, "target-1")
    navigate.assert_awaited_once_with(
        params={"url": "https://challenge.example/page"},
        session_id="session-1",
    )


async def test_sandbox_evaluate_and_get_html_note_embeds(monkeypatch) -> None:
    import openbrowse.agent.tools as tools_mod
    from openbrowse.agent.tools import _SandboxBrowser

    async def fake_eval(session, js):
        if js == tools_mod._IFRAME_HOSTS_JS:
            return ["board.example.com"]
        if "outerHTML" in js:
            return "<div>shell</div>"
        return "thin shell text"

    monkeypatch.setattr(tools_mod, "_eval_js", fake_eval)

    sb = _SandboxBrowser(None)
    body = await sb.evaluate("document.body.innerText")
    assert "MAIN page only" in body
    assert "board.example.com" in body

    title = await sb.evaluate("document.title")
    assert title == "thin shell text"

    heading = await sb.evaluate("document.querySelector('h1').textContent")
    assert heading == "thin shell text"

    html = await sb.get_html()
    assert html.startswith("<div>shell</div>")
    assert "MAIN page only" in html


async def test_lone_frame_fallback_flagged_and_retried(monkeypatch) -> None:
    good = "https://x.com/good"
    flaky = "https://x.com/flaky"
    attempts = {"n": 0}

    async def read_one(
        session, url, tid, url_contains, claimed, baseline, allow_sole_candidate=False,
        sibling_urls=None,
    ):
        if url == good:
            return {
                "url": url,
                "text": "real panel content " * 30,
                "jsonld": None,
                "links": [],
                "frame_matched": True,
            }
        attempts["n"] += 1
        if attempts["n"] == 1:
            return {
                "url": url,
                "text": "outer shell nav footer " * 30,
                "jsonld": None,
                "links": [],
            }
        return {
            "url": url,
            "text": "real panel content for flaky " * 30,
            "jsonld": None,
            "links": [],
            "frame_matched": True,
        }

    tools_mod, order, session = _wave_fakes(monkeypatch, read_one)
    progress_msgs: list[str] = []

    async def progress(msg):
        progress_msgs.append(msg)

    clipboard: dict = {}
    pages = await tools_mod._read_pages_impl(
        session, [good, flaky], "board.example.com", clipboard, progress=progress
    )
    by_url = {p["url"]: p for p in pages}
    assert not by_url[flaky].get("error")
    assert by_url[flaky].get("frame_matched") is True
    assert attempts["n"] == 2
    assert any("read the outer shell while sibling" in m for m in progress_msgs)
    assert any("recovered 1 of 1" in m for m in progress_msgs)


async def test_lone_frame_fallback_fails_honestly_when_unrecoverable(
    monkeypatch,
) -> None:
    good = "https://x.com/good"
    flaky = "https://x.com/flaky"

    async def read_one(
        session, url, tid, url_contains, claimed, baseline, allow_sole_candidate=False,
        sibling_urls=None,
    ):
        if url == good:
            return {
                "url": url,
                "text": "real panel content " * 30,
                "jsonld": None,
                "links": [],
                "frame_matched": True,
            }
        return {
            "url": url,
            "text": "outer shell nav footer " * 30,
            "jsonld": None,
            "links": [],
        }

    tools_mod, order, session = _wave_fakes(monkeypatch, read_one)
    clipboard: dict = {}
    pages = await tools_mod._read_pages_impl(
        session, [good, flaky], "board.example.com", clipboard, progress=None
    )
    by_url = {p["url"]: p for p in pages}
    err = by_url[flaky].get("error") or ""
    assert "embedding shell" in err
    assert tools_mod._frame_failure(err)
    assert tools_mod._norm_url(flaky) in clipboard["_read_failed_frame"]
    assert tools_mod._norm_url(flaky) not in clipboard["_visited"]


def test_lone_frame_fallback_needs_sibling_proof() -> None:
    from openbrowse.agent.tools import _flag_lone_frame_fallbacks

    results = {
        "https://x.com/a": {"url": "https://x.com/a", "text": "shell"},
        "https://x.com/b": {"url": "https://x.com/b", "text": "shell"},
    }
    assert _flag_lone_frame_fallbacks(results, "board.example.com") == []
    assert not any(p.get("error") for p in results.values())
    assert _flag_lone_frame_fallbacks(results, None) == []


def test_store_remove_items() -> None:
    store = _items_store()
    for name in ("A", "B", "C", "D"):
        store.add_item({"title": name, "sourceUrl": f"https://x.com/{name.lower()}"})

    ok, msg = store.remove_items([0, 2])
    assert ok
    assert "2 item(s)" in msg and "2 remain" in msg
    remaining = [it["title"] for it in store.data["items"]]
    assert remaining == ["B", "D"]

    ok, msg = store.remove_items([9])
    assert not ok and "No item at index 9" in msg
    ok, msg = store.remove_items([])
    assert not ok
    ok, msg = store.remove_items(["x"])
    assert not ok


async def test_remove_items_action_remaps_read_provenance() -> None:
    from openbrowse.agent.tools import register_output_store_tools
    from browser_use import Tools

    tools = Tools()
    store = _items_store()
    clipboard: dict = {
        "_visited": {"https://x.com/b", "https://x.com/c"},
        "_read_items": {1, 2},
    }
    for name in ("A", "B", "C"):
        store.add_item({"title": name, "sourceUrl": f"https://x.com/{name.lower()}"})
    register_output_store_tools(tools, store, clipboard)
    entry = tools.registry.registry.actions["remove_items"]
    result = await entry.function(
        params=entry.param_model(indices=[0], reason="landing-page artefact"),
        file_system=_FakeFileSystem(),
    )
    assert not result.error
    assert "landing-page artefact" in result.extracted_content
    assert [it["title"] for it in store.data["items"]] == ["B", "C"]
    assert clipboard["_read_items"] == {0, 1}

    bad = await entry.function(
        params=entry.param_model(indices=[5], reason="nope"),
        file_system=_FakeFileSystem(),
    )
    assert bad.error


async def test_store_bridge_remove_items() -> None:
    from openbrowse.agent.tools import _store_bridge

    store = _items_store()
    fs = _FakeFileSystem()
    clipboard: dict = {"_read_items": {1}}
    store.add_item({"title": "A"})
    store.add_item({"title": "B"})
    bridge = _store_bridge(store, clipboard, fs)
    msg = await bridge["remove_items"]([0], "duplicate")
    assert "1 item(s)" in msg
    assert [it["title"] for it in store.data["items"]] == ["B"]
    assert clipboard["_read_items"] == {0}


async def test_find_links_retries_recover_late_rewritten_embed_links(
    monkeypatch,
) -> None:
    import openbrowse.agent.tools as tools_mod
    from browser_use import Tools

    monkeypatch.setattr(tools_mod, "_FIND_LINKS_RETRY_DELAY_S", 0.0)

    async def fake_settle(session, frame):
        return False

    async def fake_eval(session, js):
        return "https://x.com/list"

    async def no_dom_hosts(session):
        return []

    monkeypatch.setattr(tools_mod, "_settle_lazy_links", fake_settle)
    monkeypatch.setattr(tools_mod, "_eval_js", fake_eval)
    monkeypatch.setattr(tools_mod, "_dom_iframe_hosts", no_dom_hosts)

    full_map = _embed_map(with_frame_doc=True)
    vendor_only = dict(full_map)
    for k in (11, 12):
        vendor_only[k] = _link_node(k, f"https://x.com/other{k}", target="main")
    vendor_link = _link_node(31, "https://board.example.com/", target="frame-1", text="Powered by")
    vendor_only[31] = vendor_link
    full_map = dict(full_map)
    full_map[31] = vendor_link
    maps = [vendor_only, full_map]

    class FakeSession:
        async def get_browser_state_summary(self, include_screenshot=False):
            return None

        async def get_selector_map(self):
            return maps.pop(0) if len(maps) > 1 else maps[0]

        async def get_element_by_index(self, index):
            return None

    tools = Tools()
    tools_mod.register_tab_tools(tools, object(), {}, None, None)
    entry = tools.registry.registry.actions["find_links"]
    result = await entry.function(
        browser_session=FakeSession(),
        file_system=_FakeFileSystem(),
        frame_url_contains="board.example.com",
    )
    assert not result.error
    import json as _json

    found = _json.loads(result.extracted_content)["data"]
    assert len(found) == 3
    assert "WARNING" not in (result.long_term_memory or "")


async def test_find_links_warns_when_matched_frame_stays_tiny(monkeypatch) -> None:
    import openbrowse.agent.tools as tools_mod
    from browser_use import Tools

    monkeypatch.setattr(tools_mod, "_FIND_LINKS_RETRY_DELAY_S", 0.0)

    async def fake_settle(session, frame):
        return False

    async def fake_eval(session, js):
        return "https://x.com/list"

    async def no_dom_hosts(session):
        return []

    monkeypatch.setattr(tools_mod, "_settle_lazy_links", fake_settle)
    monkeypatch.setattr(tools_mod, "_eval_js", fake_eval)
    monkeypatch.setattr(tools_mod, "_dom_iframe_hosts", no_dom_hosts)

    selector_map = _embed_map(with_frame_doc=True)
    for k in (11, 12):
        selector_map[k] = _link_node(k, f"https://x.com/other{k}", target="main")
    selector_map[31] = _link_node(
        31, "https://board.example.com/", target="frame-1", text="Powered by"
    )

    class FakeSession:
        async def get_browser_state_summary(self, include_screenshot=False):
            return None

        async def get_selector_map(self):
            return selector_map

        async def get_element_by_index(self, index):
            return None

    tools = Tools()
    tools_mod.register_tab_tools(tools, object(), {}, None, None)
    entry = tools.registry.registry.actions["find_links"]
    result = await entry.function(
        browser_session=FakeSession(),
        file_system=_FakeFileSystem(),
        frame_url_contains="board.example.com",
    )
    assert not result.error
    note = result.long_term_memory or ""
    assert "WARNING" in note
    assert "unverified" in note
    assert "re-run find_links" in note


async def test_find_links_salvages_by_href_when_frame_filter_stably_wrong(
    monkeypatch,
) -> None:
    import openbrowse.agent.tools as tools_mod
    from browser_use import Tools

    monkeypatch.setattr(tools_mod, "_FIND_LINKS_RETRY_DELAY_S", 0.0)

    async def fake_settle(session, frame):
        return False

    async def fake_eval(session, js):
        return "https://x.com/list"

    async def no_dom_hosts(session):
        return []

    monkeypatch.setattr(tools_mod, "_settle_lazy_links", fake_settle)
    monkeypatch.setattr(tools_mod, "_eval_js", fake_eval)
    monkeypatch.setattr(tools_mod, "_dom_iframe_hosts", no_dom_hosts)

    selector_map = _embed_map(with_frame_doc=True)
    for offset, k in enumerate((11, 12)):
        selector_map[k] = _link_node(
            k,
            f"https://x.com/jobs?src=board.example.com&jid={offset}",
            target="other-frame",
        )
    selector_map[31] = _link_node(
        31, "https://board.example.com/", target="frame-1", text="Powered by"
    )

    class FakeSession:
        async def get_browser_state_summary(self, include_screenshot=False):
            return None

        async def get_selector_map(self):
            return selector_map

        async def get_element_by_index(self, index):
            return None

    tools = Tools()
    clipboard: dict = {}
    tools_mod.register_tab_tools(tools, object(), clipboard, None, None)
    entry = tools.registry.registry.actions["find_links"]
    result = await entry.function(
        browser_session=FakeSession(),
        file_system=_FakeFileSystem(),
        frame_url_contains="board.example.com",
    )
    assert not result.error
    import json as _json

    found = _json.loads(result.extracted_content)["data"]
    hrefs = {l["href"] for l in found}
    assert "https://x.com/jobs?src=board.example.com&jid=0" in hrefs
    assert "https://x.com/jobs?src=board.example.com&jid=1" in hrefs
    note = result.long_term_memory or ""
    assert "recovered by matching hrefs" in note
    assert clipboard["found_links_frame"] == "board.example.com"


def test_system_metrics_pressure_levels(monkeypatch) -> None:
    import openbrowse.system_metrics as sm

    monkeypatch.setattr(sm, "_psi_cpu_some_avg10", lambda: None)
    monkeypatch.setattr(sm.os, "cpu_count", lambda: 4)
    monkeypatch.setattr(sm.os, "getloadavg", lambda: (5.5, 5.0, 4.0))
    level, s = sm.pressure()
    assert level == "saturated"
    assert s["loadPerCore"] == 1.375

    token = sm._baseline_level.set("saturated")
    note = sm.pressure_note()
    assert "saturated" in note and "environmental" in note

    sm._baseline_level.reset(token)
    note = sm.pressure_note()
    assert "own browser work" in note
    assert "environmental" not in note

    monkeypatch.setattr(sm.os, "getloadavg", lambda: (4.2, 4.0, 4.0))
    assert sm.pressure()[0] == "elevated"

    monkeypatch.setattr(sm.os, "getloadavg", lambda: (0.4, 0.5, 0.5))
    assert sm.pressure()[0] == "ok"
    assert sm.pressure_note() == ""


async def test_pressure_baseline_is_task_scoped(monkeypatch) -> None:
    import asyncio

    import openbrowse.system_metrics as sm

    monkeypatch.setattr(sm, "_psi_cpu_some_avg10", lambda: None)
    monkeypatch.setattr(sm.os, "cpu_count", lambda: 4)
    load = {"v": 0.2}
    monkeypatch.setattr(sm.os, "getloadavg", lambda: (load["v"],) * 3)

    quiet_marked = asyncio.Event()
    busy_marked = asyncio.Event()
    notes: dict[str, str] = {}

    async def quiet_launch_session():
        sm.mark_baseline()
        quiet_marked.set()
        await busy_marked.wait()
        notes["quiet"] = sm.pressure_note()

    async def busy_launch_session():
        await quiet_marked.wait()
        load["v"] = 6.0
        sm.mark_baseline()
        busy_marked.set()
        notes["busy"] = sm.pressure_note()

    await asyncio.gather(quiet_launch_session(), busy_launch_session())
    assert "own browser work" in notes["quiet"]
    assert "environmental" in notes["busy"]


async def test_shell_retry_message_carries_pressure_note(monkeypatch) -> None:
    import openbrowse.system_metrics as sm

    monkeypatch.setattr(sm, "_psi_cpu_some_avg10", lambda: None)
    monkeypatch.setattr(sm.os, "cpu_count", lambda: 4)
    monkeypatch.setattr(sm.os, "getloadavg", lambda: (6.5, 6.0, 6.0))

    async def read_one(
        session, url, tid, url_contains, claimed, baseline, allow_sole_candidate=False,
        sibling_urls=None,
    ):
        return {"url": url, "text": "identical shell text " * 30, "jsonld": None, "links": []}

    tools_mod, order, session = _wave_fakes(monkeypatch, read_one)

    async def dom_hosts(session_):
        return ["board.example.com"]

    monkeypatch.setattr(tools_mod, "_dom_iframe_hosts", dom_hosts)
    progress_msgs: list[str] = []

    async def progress(msg):
        progress_msgs.append(msg)

    urls = [f"https://x.com/{i}" for i in range(3)]
    await tools_mod._read_pages_impl(session, urls, None, {}, progress=progress)
    stamped = [m for m in progress_msgs if "host CPU saturated" in m]
    assert stamped, progress_msgs
    assert "own browser work" in stamped[0]


def _stagger_harness(monkeypatch):
    async def read_one(
        session, url, tid, url_contains, claimed, baseline, allow_sole_candidate=False,
        sibling_urls=None,
    ):
        return {"url": url, "text": f"body of {url} " * 30, "jsonld": None, "links": []}

    tools_mod, order, session = _wave_fakes(monkeypatch, read_one)
    pauses: list[float] = []

    async def fake_pause(seconds):
        pauses.append(seconds)

    monkeypatch.setattr(tools_mod, "_stagger_pause", fake_pause)
    return tools_mod, session, pauses


async def test_wave_stagger_zero_when_solo(monkeypatch) -> None:
    tools_mod, session, pauses = _stagger_harness(monkeypatch)
    monkeypatch.setattr(tools_mod.system_metrics, "stall_fraction", lambda: 1.0)
    monkeypatch.setattr(tools_mod.activity, "active_session_count", lambda: 1)
    await tools_mod._read_pages_impl(session, [f"https://x.com/{i}" for i in range(3)], None, {})
    assert pauses == []


async def test_wave_stagger_zero_when_calm(monkeypatch) -> None:
    tools_mod, session, pauses = _stagger_harness(monkeypatch)
    monkeypatch.setattr(tools_mod.system_metrics, "stall_fraction", lambda: 0.02)
    monkeypatch.setattr(tools_mod.activity, "active_session_count", lambda: 3)
    await tools_mod._read_pages_impl(session, [f"https://x.com/{i}" for i in range(3)], None, {})
    assert pauses == []


async def test_wave_stagger_paces_between_spawns_under_contention(monkeypatch) -> None:
    tools_mod, session, pauses = _stagger_harness(monkeypatch)
    monkeypatch.setattr(tools_mod.system_metrics, "stall_fraction", lambda: 0.5)
    monkeypatch.setattr(tools_mod.activity, "active_session_count", lambda: 2)
    progress_msgs: list[str] = []

    async def progress(msg):
        progress_msgs.append(msg)

    await tools_mod._read_pages_impl(
        session, [f"https://x.com/{i}" for i in range(3)], None, {}, progress=progress
    )
    assert pauses == [pytest.approx(0.4), pytest.approx(0.4)]
    assert any("paced 400ms/tab" in m for m in progress_msgs), progress_msgs


async def test_wave_stagger_total_is_capped(monkeypatch) -> None:
    tools_mod, session, pauses = _stagger_harness(monkeypatch)
    monkeypatch.setattr(tools_mod.system_metrics, "stall_fraction", lambda: 1.0)
    monkeypatch.setattr(tools_mod.activity, "active_session_count", lambda: 2)
    urls = [f"https://x.com/{i}" for i in range(8)]
    await tools_mod._read_pages_impl(session, urls, None, {}, concurrency=8)
    assert len(pauses) == 7
    assert sum(pauses) <= tools_mod._STAGGER_TOTAL_MAX_S + 1e-9
    assert all(p == pytest.approx(tools_mod._STAGGER_PER_TAB_MAX_S) for p in pauses)


async def test_wave_stagger_skips_single_url_waves(monkeypatch) -> None:
    tools_mod, session, pauses = _stagger_harness(monkeypatch)
    monkeypatch.setattr(tools_mod.system_metrics, "stall_fraction", lambda: 1.0)
    monkeypatch.setattr(tools_mod.activity, "active_session_count", lambda: 4)
    await tools_mod._read_pages_impl(session, ["https://x.com/1"], None, {})
    assert pauses == []


def test_psi_parse_and_stall_fraction(tmp_path, monkeypatch) -> None:
    import openbrowse.system_metrics as sm

    psi = tmp_path / "cpu"
    psi.write_text(
        "some avg10=23.50 avg60=10.00 avg300=3.00 total=123456\n"
        "full avg10=1.00 avg60=0.50 avg300=0.10 total=6543\n"
    )
    monkeypatch.setattr(sm, "_PSI_CPU_PATH", str(psi))
    assert sm._psi_cpu_some_avg10() == 23.5
    assert sm.stall_fraction() == pytest.approx(0.235)
    assert sm.sample()["cpuStallPct"] == 23.5

    level, _ = sm.pressure()
    assert level == "elevated"
    assert "% stall" in sm.pressure_note()

    psi.write_text("some avg10=45.00 avg60=20.00 avg300=5.00 total=1\n")
    assert sm.pressure()[0] == "saturated"

    psi.write_text("some avg10=2.00 avg60=1.00 avg300=0.00 total=1\n")
    monkeypatch.setattr(sm.os, "getloadavg", lambda: (9.0, 9.0, 9.0))
    assert sm.pressure()[0] == "ok"


def test_stall_fraction_loadavg_fallback(tmp_path, monkeypatch) -> None:
    import openbrowse.system_metrics as sm

    monkeypatch.setattr(sm, "_PSI_CPU_PATH", str(tmp_path / "missing"))
    monkeypatch.setattr(sm.os, "cpu_count", lambda: 4)

    monkeypatch.setattr(sm.os, "getloadavg", lambda: (2.0, 2.0, 2.0))
    assert sm.stall_fraction() == 0.0

    monkeypatch.setattr(sm.os, "getloadavg", lambda: (6.0, 6.0, 6.0))
    assert sm.stall_fraction() == pytest.approx(0.5)

    monkeypatch.setattr(sm.os, "getloadavg", lambda: (12.0, 12.0, 12.0))
    assert sm.stall_fraction() == 1.0
    assert sm.sample()["cpuStallPct"] is None


async def test_find_links_relaxes_starving_caller_filters(monkeypatch) -> None:
    import openbrowse.agent.tools as tools_mod
    from browser_use import Tools

    monkeypatch.setattr(tools_mod, "_FIND_LINKS_RETRY_DELAY_S", 0.0)

    async def fake_settle(session, frame):
        return False

    async def fake_eval(session, js):
        return "https://x.com/list"

    async def no_dom_hosts(session):
        return []

    monkeypatch.setattr(tools_mod, "_settle_lazy_links", fake_settle)
    monkeypatch.setattr(tools_mod, "_eval_js", fake_eval)
    monkeypatch.setattr(tools_mod, "_dom_iframe_hosts", no_dom_hosts)

    selector_map = _embed_map(with_frame_doc=True)
    selector_map[31] = _link_node(
        31, "https://board.example.com/", target="frame-1", text="Powered by"
    )

    class FakeSession:
        async def get_browser_state_summary(self, include_screenshot=False):
            return None

        async def get_selector_map(self):
            return selector_map

        async def get_element_by_index(self, index):
            return None

    tools = Tools()
    tools_mod.register_tab_tools(tools, object(), {}, None, None)
    entry = tools.registry.registry.actions["find_links"]
    result = await entry.function(
        browser_session=FakeSession(),
        file_system=_FakeFileSystem(),
        frame_url_contains="board.example.com",
        href_regex=r"ashbyhq\.com|jobs\.ashbyhq",
    )
    assert not result.error
    import json as _json

    found = _json.loads(result.extracted_content)["data"]
    assert len(found) == 3
    assert any("embed_jid=aaa" in l["href"] for l in found)
    note = result.long_term_memory or ""
    assert "kept 0 of 3" in note
    assert "rewrites its anchors to the host page's own URLs" in note
    assert "embed_jid" in note


async def test_gate_bounce_has_no_termination_vocabulary() -> None:
    from openbrowse.agent.tools import register_completeness_gate
    from browser_use import Tools

    tools = Tools()
    store = _hints_store()
    store.add_item({"title": "A"})
    register_completeness_gate(tools, store, None)
    entry = tools.registry.registry.actions["done"]
    params = entry.param_model(text="all done", success=True)

    first = await entry.function(params=params, file_system=_FakeFileSystem())
    assert first.is_done is False
    text = first.extracted_content
    assert "execution limit" not in text
    assert "stop early" not in text
    assert "Do the work above first" in text
    assert text.index("Do the work above first") > text.index("mark_absent")


def test_draft_row_rejected_visible_label_never_pollutes_weaker_field() -> None:
    from openbrowse.agent.output_store import OutputStore
    from openbrowse.agent.schema import json_schema_to_pydantic
    from openbrowse.agent.tools import _draft_row, _labelled_pairs

    schema = {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["title"],
                    "properties": {
                        "title": {"type": "string"},
                        "locationType": {
                            "anyOf": [
                                {"type": "string", "enum": ["ONSITE", "HYBRID", "REMOTE"]},
                                {"type": "null"},
                            ]
                        },
                        "location": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                    },
                },
            }
        },
    }
    store = OutputStore(json_schema_to_pydantic(schema))
    text = "Location Type\nFully Flexible Setup\n" + "body filler " * 60
    assert _labelled_pairs(text).get("Location Type") == "Fully Flexible Setup"
    page = {
        "url": "https://x.com/jobs?id=2",
        "title": "Role Two",
        "text": text,
        "jsonld": None,
    }
    import json as _json

    row = _draft_row(store, page)
    assert "locationType" not in row
    assert row.get("location") != "Fully Flexible Setup"
    declared = {k: v for k, v in row.items() if k != "extra"}
    assert "Fully Flexible" not in _json.dumps(declared)


def test_draft_row_rejected_enum_value_never_pollutes_weaker_field() -> None:
    from openbrowse.agent.output_store import OutputStore
    from openbrowse.agent.schema import json_schema_to_pydantic
    from openbrowse.agent.tools import _draft_row

    schema = {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["title"],
                    "properties": {
                        "title": {"type": "string"},
                        "locationType": {
                            "anyOf": [
                                {"type": "string", "enum": ["ONSITE", "HYBRID", "REMOTE"]},
                                {"type": "null"},
                            ]
                        },
                        "location": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                    },
                },
            }
        },
    }
    store = OutputStore(json_schema_to_pydantic(schema))
    page = {
        "url": "https://x.com/jobs?id=1",
        "title": "Role One",
        "text": "t" * 500,
        "jsonld": {
            "@type": "JobPosting",
            "title": "Role One",
            "jobLocationType": "TELECOMMUTE",
            "jobLocation": {
                "@type": "Place",
                "address": {"@type": "PostalAddress", "addressLocality": "London"},
            },
        },
    }
    import json as _json

    row = _draft_row(store, page)
    assert row.get("location") == "London"
    assert "locationType" not in row
    declared = {k: v for k, v in row.items() if k != "extra"}
    assert "TELECOMMUTE" not in _json.dumps(declared)


def test_draft_row_harvests_undeclared_extra_when_schema_allows() -> None:
    import json as _json

    from openbrowse.agent.output_store import OutputStore
    from openbrowse.agent.schema import json_schema_to_pydantic
    from openbrowse.agent.tools import _draft_row

    schema = {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["title"],
                    "properties": {
                        "title": {"type": "string"},
                        "sourceUrl": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                    },
                    "additionalProperties": {},
                },
            }
        },
    }
    store = OutputStore(json_schema_to_pydantic(schema))
    page = {
        "url": "https://x.com/p?id=1",
        "title": "Role One",
        "text": "t" * 500,
        "jsonld": {
            "@type": "JobPosting",
            "title": "Role One",
            "employmentType": "FullTime",
            "workplaceType": "Hybrid",
        },
    }
    row = _draft_row(store, page)
    extra = row.get("extra")
    assert isinstance(extra, list) and extra
    keys = {e["key"] for e in extra}
    assert "employmentType" in keys or "workplaceType" in keys

    ok, msg = store.add_item(row)
    assert ok, msg
    stored = _json.loads(store.read_output())["items"][0]
    assert stored.get("extra")


def _visual_store():
    from openbrowse.agent.output_store import OutputStore
    from openbrowse.agent.schema import json_schema_to_pydantic

    schema = {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["title"],
                    "properties": {
                        "title": {"type": "string"},
                        "location": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                        "category": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                        "publishedAt": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                    },
                },
            }
        },
    }
    return OutputStore(json_schema_to_pydantic(schema))


def test_draft_row_rendered_values_outrank_background_data() -> None:
    from openbrowse.agent.tools import _draft_row

    store = _visual_store()
    page = {
        "url": "https://x.com/p?id=1",
        "title": "Role One",
        "text": "Role One\nLocation\n\nLondon\n\nOverview\n" + "t" * 400,
        "jsonld": {
            "@type": "JobPosting",
            "title": "Role One",
            "datePublished": "2026-08-04",
            "jobLocation": {
                "@type": "Place",
                "address": {
                    "@type": "PostalAddress",
                    "addressCountry": "United Kingdom",
                },
            },
        },
    }
    row = _draft_row(store, page)
    assert row.get("location") == "London"
    assert row.get("publishedAt") == "2026-08-04"


def test_draft_row_background_upgrades_visual_only_when_richer() -> None:
    from openbrowse.agent.tools import _draft_row

    store = _visual_store()
    page = {
        "url": "https://x.com/p?id=2",
        "title": "Role Two",
        "text": "Role Two\nCategory\n\nHome\n\nLocation\n\nLondon\n\nOverview\n" + "t" * 400,
        "jsonld": {
            "@type": "Thing",
            "title": "Role Two",
            "category": "Home Office",
            "jobLocation": {
                "@type": "Place",
                "address": {
                    "@type": "PostalAddress",
                    "addressCountry": "United Kingdom",
                },
            },
        },
    }
    row = _draft_row(store, page)
    assert row.get("category") == "Home Office"
    assert row.get("location") == "London"


async def test_read_pages_targets_sole_embed_host_without_frame_filter(
    monkeypatch,
) -> None:
    seen_filters: list = []

    async def read_one(
        session, url, tid, url_contains, claimed, baseline, allow_sole_candidate=False,
        sibling_urls=None,
    ):
        seen_filters.append(url_contains)
        return {
            "url": url,
            "text": "panel content " * 30,
            "jsonld": None,
            "links": [],
            "frame_matched": True,
        }

    tools_mod, order, session = _wave_fakes(monkeypatch, read_one)

    async def one_host(session_):
        return ["panel.example.com"]

    monkeypatch.setattr(tools_mod, "_dom_panel_iframe_hosts", one_host)
    progress_msgs: list[str] = []

    async def progress(msg):
        progress_msgs.append(msg)

    await tools_mod._read_pages_impl(
        session, ["https://x.com/a"], None, {}, progress=progress
    )
    assert seen_filters == ["panel.example.com"]
    assert any("single cross-origin panel host" in m for m in progress_msgs)


async def test_read_pages_ignores_sole_widget_sized_frame(monkeypatch) -> None:
    seen_filters: list = []

    async def read_one(
        session, url, tid, url_contains, claimed, baseline, allow_sole_candidate=False,
        sibling_urls=None,
    ):
        seen_filters.append(url_contains)
        return {"url": url, "text": "body " * 60, "jsonld": None, "links": []}

    tools_mod, order, session = _wave_fakes(monkeypatch, read_one)

    async def chat_widget_only(session_):
        return ["widget.chat-vendor.com"]

    async def no_panel_hosts(session_):
        return []

    monkeypatch.setattr(tools_mod, "_dom_iframe_hosts", chat_widget_only)
    monkeypatch.setattr(tools_mod, "_dom_panel_iframe_hosts", no_panel_hosts)
    await tools_mod._read_pages_impl(session, ["https://x.com/a"], None, {})
    assert seen_filters == [None]


async def test_read_pages_keeps_frameless_when_hosts_ambiguous(monkeypatch) -> None:
    seen_filters: list = []

    async def read_one(
        session, url, tid, url_contains, claimed, baseline, allow_sole_candidate=False,
        sibling_urls=None,
    ):
        seen_filters.append(url_contains)
        return {"url": url, "text": "body " * 60, "jsonld": None, "links": []}

    tools_mod, order, session = _wave_fakes(monkeypatch, read_one)

    async def two_hosts(session_):
        return ["a.example.com", "b.example.com"]

    monkeypatch.setattr(tools_mod, "_dom_panel_iframe_hosts", two_hosts)
    await tools_mod._read_pages_impl(session, ["https://x.com/a"], None, {})
    assert seen_filters == [None]


async def test_read_one_page_waits_for_panel_seen_in_dom(monkeypatch) -> None:
    import openbrowse.agent.tools as tools_mod

    match_calls = {"n": 0}

    async def fake_match(
        session, tid, needle, claimed, baseline, allow_sole=False, page_url=None,
        sibling_urls=None,
    ):
        match_calls["n"] += 1
        return "frame-1" if match_calls["n"] >= 4 else None

    async def fake_eval(session, tid, js):
        if js == tools_mod._IFRAME_SRC_JS:
            return ["https://board.example.com/acme/embed"]
        if js == tools_mod._BODY_TEXT_JS:
            return "panel content " * 30
        if js == tools_mod._JSONLD_JS:
            return []
        if js == tools_mod._LINKS_JS:
            return []
        if "readyState" in js:
            return "complete"
        return "Title"

    monkeypatch.setattr(tools_mod, "_match_frame_target", fake_match)
    monkeypatch.setattr(tools_mod, "_eval_on_target", fake_eval)
    monkeypatch.setattr(tools_mod, "_FRAME_MATCH_GRACE_S", 0.0)
    monkeypatch.setattr(tools_mod, "_JSONLD_GRACE_S", 0.0)

    page = await tools_mod._read_one_page(
        None, "https://x.com/j1", "tid-1", "board.example.com", set(), set()
    )
    assert page.get("frame_matched") is True
    assert not page.get("error")
    assert match_calls["n"] >= 4


async def test_read_one_page_keeps_filter_when_needle_only_in_query(
    monkeypatch,
) -> None:
    import openbrowse.agent.tools as tools_mod

    async def fake_match(
        session, tid, needle, claimed, baseline, allow_sole=False, page_url=None,
        sibling_urls=None,
    ):
        return "frame-1"

    async def fake_eval(session, tid, js):
        if js == tools_mod._BODY_TEXT_JS:
            return "panel content " * 30
        if js == tools_mod._JSONLD_JS:
            return []
        if js == tools_mod._LINKS_JS:
            return []
        if "readyState" in js:
            return "complete"
        return "Title"

    monkeypatch.setattr(tools_mod, "_match_frame_target", fake_match)
    monkeypatch.setattr(tools_mod, "_eval_on_target", fake_eval)
    monkeypatch.setattr(tools_mod, "_JSONLD_GRACE_S", 0.0)

    page = await tools_mod._read_one_page(
        None,
        "https://x.com/jobs?board_jid=abc123#openings",
        "tid-1",
        "board",
        set(),
        set(),
    )
    assert page.get("frame_matched") is True
    assert not page.get("frame_skipped_own_host")
    assert not page.get("error")


def test_tolerate_json_list_shapes() -> None:
    from openbrowse.agent.tools import _tolerate_json_list

    assert _tolerate_json_list('["a", "b"]') == ["a", "b"]
    assert _tolerate_json_list(["a"]) == ["a"]
    assert _tolerate_json_list("plainField") == "plainField"
    assert _tolerate_json_list("[not json") == "[not json"
    assert _tolerate_json_list(None) is None


async def test_mark_absent_accepts_json_string_list() -> None:
    """Claude's observed wire drift: a list argument serialised as its JSON
    text must settle every named field, not bounce as one unknown field."""
    from openbrowse.agent.tools import register_output_store_tools

    tools = Tools()
    store = _items_store()
    clipboard = {"_visited": {"https://x.com/a"}}
    store.add_item({"title": "A", "sourceUrl": "https://x.com/a"})
    register_output_store_tools(tools, store, clipboard)
    entry = tools.registry.registry.actions["mark_absent"]
    params = entry.param_model(
        field='["description"]', reason="never published anywhere"
    )
    result = await entry.function(params=params, file_system=_FakeFileSystem())
    assert not result.error, result.error
    assert "description" in store.absent_fields
    assert '["description"]' not in store.absent_fields


async def test_search_page_flow_wrapper() -> None:
    from types import SimpleNamespace

    from browser_use import ActionResult

    from openbrowse.agent.tools import register_search_page_flow

    calls = []

    async def fake_search(params=None, **kwargs):
        calls.append(params.css_scope)
        return ActionResult(extracted_content="2 matches found")

    tools = Tools()
    entry = tools.registry.registry.actions.get("search_page")
    if entry is None:
        import pytest

        pytest.skip("browser-use build has no search_page action")
    entry.function = fake_search
    clipboard = {"_visited": {"https://x.com/a"}}
    register_search_page_flow(tools, clipboard)

    from openbrowse.agent.tools import model_visible_attrs

    def seen_by_model(result) -> str:
        return "\n".join(str(getattr(result, a) or "") for a in model_visible_attrs(result))

    params = SimpleNamespace(pattern="salary|equity", css_scope="null")
    first = await entry.function(params=params)
    assert calls[-1] is None
    assert "pages.json" in seen_by_model(first)

    params2 = SimpleNamespace(pattern="salary|equity", css_scope=None)
    second = await entry.function(params=params2)
    assert "searched 2 times" in seen_by_model(second)
    assert "run_code_file" in seen_by_model(second)


async def test_read_output_fields_json_string_normalised_at_boundary() -> None:
    """The strict signature rejects a stringified list; the boundary normaliser
    repairs it before validation, so the pair must round-trip."""
    from openbrowse.agent.leak_repair import coerce_action_param_shapes
    from openbrowse.agent.tools import action_param_kinds, register_output_store_tools

    tools = Tools()
    store = _items_store()
    store.add_item({"title": "A", "sourceUrl": "https://x.com/a"})
    register_output_store_tools(tools, store, {})
    kinds = action_param_kinds(tools)
    ti = {"action": [{"read_output": {"index": 0, "fields": '["title"]'}}]}
    assert coerce_action_param_shapes(ti, kinds) is True
    entry = tools.registry.registry.actions["read_output"]
    params = entry.param_model(**ti["action"][0]["read_output"])
    result = await entry.function(params=params, file_system=_FakeFileSystem())
    assert not result.error, result.error
    assert "A" in (result.extracted_content or "")


def test_action_param_kinds_map() -> None:
    from openbrowse.agent.tools import _param_kind, action_param_kinds, register_output_store_tools

    tools = Tools()
    register_output_store_tools(tools, _items_store(), {})
    kinds = action_param_kinds(tools)
    assert kinds["mark_absent"]["field"]["container"] == "list"
    assert kinds["read_output"]["fields"]["container"] == "list"
    assert _param_kind(list[str]) == {"container": "list", "elem": "str", "optional": False}
    assert _param_kind(dict[str, str] | None) == {"container": "dict", "elem": None, "optional": True}
    assert _param_kind(int | None) == {"container": None, "elem": None, "optional": True}
    assert _param_kind(str | None) == {
        "container": None,
        "elem": None,
        "optional": True,
        "plain_str": True,
    }
    assert _param_kind(str) is None
    assert _param_kind(list[int]) == {"container": "list", "elem": "int", "optional": False}


async def test_remember_rejects_reserved_keys() -> None:
    from openbrowse.agent.tools import register_clipboard_tools

    tools = Tools()
    clipboard = {"found_links": ["https://x.com/a"]}
    register_clipboard_tools(tools, clipboard)
    entry = tools.registry.registry.actions["remember"]
    params = entry.param_model(key="found_links", value="oops")
    result = await entry.function(params=params)
    assert result.error and "internal session key" in result.error
    assert clipboard["found_links"] == ["https://x.com/a"]


def test_saved_links_survive_corruption() -> None:
    from openbrowse.agent.tools import _saved_links_sans_offhost

    kept, skipped = _saved_links_sans_offhost({"found_links": "https://x.com/a"})
    assert kept == [] and skipped == 0
    kept, _ = _saved_links_sans_offhost(
        {"found_links": ["https://a", "https://b"], "found_links_offhost": "https"}
    )
    assert kept == ["https://a", "https://b"]


def test_filter_page_urls() -> None:
    from openbrowse.agent.tools import _filter_page_urls

    kept, dropped = _filter_page_urls(["null", "https://x.com/a", "jobs", "http://y.com"])
    assert kept == ["https://x.com/a", "http://y.com"] and dropped == 2
    kept, dropped = _filter_page_urls(None)
    assert kept is None and dropped == 0
    kept, dropped = _filter_page_urls(["null"])
    assert kept == [] and dropped == 1


def test_coerce_scalar_unwraps_json_strings_for_container_fields() -> None:
    from openbrowse.agent.output_store import _coerce_scalar

    assert _coerce_scalar('["a", "b"]', list[str]) == ["a", "b"]
    assert _coerce_scalar('{"k": "v"}', dict[str, str]) == {"k": "v"}
    assert _coerce_scalar('["a"]', str) == '["a"]'
    assert _coerce_scalar("plain", list[str]) == "plain"


async def test_gate_emits_pass_event_on_clean_done() -> None:
    from openbrowse.agent.tools import register_completeness_gate, register_output_store_tools

    tools = Tools()
    store = _items_store()
    store.add_item(
        {"title": "A", "sourceUrl": "https://x.com/a", "description": "d" * 40}
    )
    register_output_store_tools(tools, store, {"_visited": {"https://x.com/a"}})
    passes: list[str] = []
    bounces: list[list[str]] = []

    async def on_incomplete(fields):
        bounces.append(fields)

    async def on_complete(coverage):
        passes.append(coverage)

    register_completeness_gate(tools, store, on_incomplete, {}, on_complete)
    entry = tools.registry.registry.actions["done"]
    params = entry.param_model(text="all collected", success=True)
    result = await entry.function(params=params, file_system=_FakeFileSystem())
    assert result.is_done, getattr(result, "extracted_content", "")
    assert passes and not bounces


# --- delivery channel: what the agent can actually read ---------------------


def _seen_by_model(result) -> str:
    from openbrowse.agent.tools import model_visible_attrs

    return "\n".join(str(getattr(result, a) or "") for a in model_visible_attrs(result))


def _delivered(result):
    """Parse a `deliver`-shaped reply: the whole field is one JSON envelope."""
    import json as _json

    return _json.loads(_seen_by_model(result))


def _delivered_data(result):
    return _delivered(result)["data"]


async def test_model_visible_attrs_matches_browser_use_history_rendering() -> None:
    """The contract every other fix here rests on. If browser-use ever changes which
    fields it forwards, this fails before the tools quietly stop being readable."""
    import tempfile

    from browser_use.agent.message_manager.service import MessageManager
    from browser_use.agent.views import AgentStepInfo
    from browser_use.filesystem.file_system import FileSystem
    from browser_use.llm.messages import SystemMessage

    from openbrowse.agent.tools import model_visible_attrs

    fs = FileSystem(tempfile.mkdtemp())
    cases = [
        ActionResult(extracted_content="LISTING", long_term_memory="COUNT"),
        ActionResult(extracted_content="LISTING"),
        ActionResult(long_term_memory="COUNT"),
        ActionResult(
            extracted_content="LISTING",
            long_term_memory="COUNT",
            include_extracted_content_only_once=True,
        ),
    ]
    for result in cases:
        manager = MessageManager(
            task="t", system_message=SystemMessage(content="s"), file_system=fs
        )
        manager._update_agent_history_description(
            None, [result], AgentStepInfo(step_number=0, max_steps=10)
        )
        rendered = "\n".join(i.to_string() for i in manager.state.agent_history_items)
        rendered += manager.state.read_state_description
        for attr in ("extracted_content", "long_term_memory"):
            text = getattr(result, attr, None)
            if not text:
                continue
            forwarded = text in rendered
            predicted = attr in model_visible_attrs(result)
            assert forwarded == predicted, (attr, result)


async def test_find_elements_asks_for_the_attributes_its_selector_names() -> None:
    """A selector that filters on href and comes back with tag and text only is a dead
    end: the href is unrecoverable and nothing says it was withheld."""
    from types import SimpleNamespace

    import openbrowse.agent.tools as tools_mod
    from openbrowse.agent.tools import register_find_elements_flow

    ran: list[str] = []

    async def fake_eval(session, js):
        ran.append(js)
        return {
            "elements": [
                {"index": i, "tag": "a", "text": "X", "attrs": {"href": f"https://x.com/{i}"}}
                for i in range(3)
            ],
            "total": 3,
            "showing": 3,
        }

    tools = Tools()
    entry = tools.registry.registry.actions.get("find_elements")
    if entry is None:
        pytest.skip("browser-use build has no find_elements action")
    register_find_elements_flow(tools)

    import unittest.mock as _mock

    with _mock.patch.object(tools_mod, "_eval_js", fake_eval):
        params = SimpleNamespace(
            selector="a[href*='twitter.com'], a[href*='linkedin.com']",
            attributes=None,
            max_results=50,
            include_text=True,
        )
        result = await entry.function(
            params=params, browser_session=object(), file_system=_FakeFileSystem()
        )

    assert '"href"' in ran[-1], "the derived attribute must reach the query"
    data = _delivered_data(result)
    assert [e["attrs"]["href"] for e in data] == [f"https://x.com/{i}" for i in range(3)]


async def test_find_elements_respects_attributes_the_caller_gave() -> None:
    from types import SimpleNamespace

    import openbrowse.agent.tools as tools_mod
    from openbrowse.agent.tools import register_find_elements_flow

    ran: list[str] = []

    async def fake_eval(session, js):
        ran.append(js)
        return {"elements": [], "total": 0, "showing": 0}

    tools = Tools()
    entry = tools.registry.registry.actions.get("find_elements")
    if entry is None:
        pytest.skip("browser-use build has no find_elements action")
    register_find_elements_flow(tools)

    import unittest.mock as _mock

    with _mock.patch.object(tools_mod, "_eval_js", fake_eval):
        params = SimpleNamespace(
            selector="a[href]", attributes=["src"], max_results=50, include_text=True
        )
        await entry.function(
            params=params, browser_session=object(), file_system=_FakeFileSystem()
        )
    assert '"src"' in ran[-1]
    assert '"href"' not in ran[-1].split("ATTRIBUTES")[1].split(";")[0]


def test_attrs_from_selector_covers_filters_and_tag_identity() -> None:
    from openbrowse.agent.tools import _attrs_from_selector

    assert _attrs_from_selector("a[href*='x.com']") == ["href"]
    assert _attrs_from_selector("img[data-src]") == ["data-src", "src"]
    assert _attrs_from_selector("div.card") == []
    assert _attrs_from_selector("a") == ["href"]
    assert "href" in _attrs_from_selector("nav a, footer a")


async def test_recall_returns_the_whole_value_not_a_hundred_characters() -> None:
    """recall is the documented way back to stashed data; a silent 100-char preview
    makes every pointer that names it a lie."""
    from openbrowse.agent.tools import register_clipboard_tools

    links = [f"https://example.com/page-{i}" for i in range(40)]
    clipboard = {"found_links": links}
    tools = Tools()
    register_clipboard_tools(tools, clipboard)
    entry = tools.registry.registry.actions["recall"]

    result = await entry.function(key="found_links", file_system=_FakeFileSystem())
    visible = _seen_by_model(result)

    assert len(str(links)) > 100
    assert "page-0" in visible and "page-39" in visible


async def test_recall_spills_a_big_value_to_a_file_and_names_it() -> None:
    """Over the budget the value goes to a file and the reply says how to read it, so
    nothing is silently cut."""
    from openbrowse.agent.tools import INLINE_BUDGET, register_clipboard_tools

    value = ["https://example.com/a-fairly-long-url-for-padding"] * 200
    tools = Tools()
    register_clipboard_tools(tools, {"blob": value})
    entry = tools.registry.registry.actions["recall"]

    fs = _FakeFileSystem()
    envelope = _delivered(await entry.function(key="blob", file_system=fs))

    assert envelope["truncated"] is True
    assert envelope["total_chars"] > INLINE_BUDGET
    assert "read_file(" in envelope["read_with"]
    saved = envelope["file"]
    import json as _json

    assert _json.loads(fs.files[saved]) == value, "the file must hold the whole value"


async def test_output_guard_breaks_a_repeat_loop_on_the_persistent_channel() -> None:
    """The wise.com shape: a short constant reply, under every size threshold, that an
    agent can re-request forever without learning anything."""
    from openbrowse.agent.tools import register_output_guard_overrides

    async def fake(params=None, **kwargs):
        return ActionResult(long_term_memory='Found 5 elements matching "a[href]".')

    tools = Tools()
    entry = tools.registry.registry.actions.get("find_elements")
    if entry is None:
        pytest.skip("browser-use build has no find_elements action")
    entry.function = fake
    register_output_guard_overrides(tools)

    first = _seen_by_model(await entry.function(params=None))
    assert "STOP REPEATING" not in first

    second = _seen_by_model(await entry.function(params=None))
    assert "STOP REPEATING THIS" in second
    assert "find_elements" in second


async def test_output_guard_back_reference_names_the_file_it_saved() -> None:
    from openbrowse.agent.tools import register_output_guard_overrides

    big = "y" * 20000

    async def fake(params=None, **kwargs):
        return ActionResult(long_term_memory=big)

    tools = Tools()
    entry = tools.registry.registry.actions.get("evaluate")
    if entry is None:
        pytest.skip("browser-use build has no evaluate action")
    entry.function = fake
    register_output_guard_overrides(tools)

    fs = _FakeFileSystem()
    first = _seen_by_model(await entry.function(params=None, file_system=fs))
    saved_as = first.split("saved to '")[1].split("'")[0]
    assert saved_as.startswith("readout_evaluate")

    second = _seen_by_model(await entry.function(params=None, file_system=fs))
    assert saved_as in second, "a back-reference must say where to look"


async def test_read_pages_queues_the_remainder_and_resumes_from_it(monkeypatch) -> None:
    """Over the cap, a second no-args call used to re-read the same first batch while
    the result claimed everything had been read."""
    import openbrowse.agent.tools as tools_mod
    from openbrowse.agent.tools import register_tab_tools

    calls: list[list[str]] = []

    async def fake_impl(session, urls, url_contains, clipboard, progress=None):
        calls.append(list(urls))
        return [
            {"url": u, "title": "t", "text": "body " * 100, "jsonld": None, "links": []}
            for u in urls
        ]

    monkeypatch.setattr(tools_mod, "_read_pages_impl", fake_impl)
    monkeypatch.setattr(tools_mod, "_READ_PAGES_MAX", 3)

    all_urls = [f"https://x.com/{i}" for i in range(5)]
    clipboard: dict = {"found_links": list(all_urls)}
    tools = Tools()
    register_tab_tools(tools, object(), clipboard, _items_store(), None)
    entry = tools.registry.registry.actions["read_pages"]

    first = await entry.function(browser_session=object(), file_system=_FakeFileSystem())
    assert calls[-1] == all_urls[:3]
    visible = _seen_by_model(first)
    assert "2 link(s) are still UNREAD" in visible
    assert "NO arguments" in visible

    second = await entry.function(browser_session=object(), file_system=_FakeFileSystem())
    assert calls[-1] == all_urls[3:], "the second call must resume, not repeat"
    assert "Nothing is queued" in _seen_by_model(second)


async def test_read_pages_says_plainly_when_the_queue_is_empty(monkeypatch) -> None:
    import openbrowse.agent.tools as tools_mod
    from openbrowse.agent.tools import register_tab_tools

    async def fake_impl(session, urls, url_contains, clipboard, progress=None):
        return [
            {"url": u, "title": "t", "text": "body " * 100, "jsonld": None, "links": []}
            for u in urls
        ]

    monkeypatch.setattr(tools_mod, "_read_pages_impl", fake_impl)

    clipboard: dict = {
        "found_links": ["https://x.com/a"],
        "_unread_links": ["https://x.com/a"],
        "_visited": {tools_mod._norm_url("https://x.com/a")},
    }
    tools = Tools()
    register_tab_tools(tools, object(), clipboard, _items_store(), None)
    entry = tools.registry.registry.actions["read_pages"]

    result = await entry.function(browser_session=object(), file_system=_FakeFileSystem())
    assert "nothing left to read" in _seen_by_model(result)


def test_gate_link_deficit_reports_the_links_it_does_not_list() -> None:
    """The gate fires once, so a silent top-8 cut lets an agent clear eight links and
    believe it is finished."""
    from openbrowse.agent.tools import _gate_link_deficit

    store = _items_store()
    clipboard = {"found_links": [f"https://x.com/{i}" for i in range(20)]}
    msg = _gate_link_deficit(store, clipboard)

    assert msg is not None
    assert "and 12 more link(s)" in msg




async def test_sandbox_crash_says_which_files_survived_where_the_model_can_read_it(
    tmp_path,
) -> None:
    """Which files survived matters most when the script crashed, and `error` is the
    one field that cannot carry it."""
    result = await _run_sandbox(
        tmp_path, "save_json({'a': 1}, 'kept.json')\nraise ValueError('boom')"
    )
    assert result.error
    visible = _seen_by_model(result)
    assert "kept.json" in visible


def test_clip_marked_says_when_it_cut_something() -> None:
    from openbrowse.agent.tools import _clip_marked

    short = "fits fine"
    assert _clip_marked(short) == short

    long_value = "z" * 900
    clipped = _clip_marked(long_value)
    assert clipped.startswith("z" * 500)
    assert "900 chars total" in clipped
    assert "cut here" in clipped


# --- regressions from review of the channel fix ----------------------------


def _queue_fixture(monkeypatch, count=5, cap=3, verbose=False):
    import openbrowse.agent.tools as tools_mod
    from openbrowse.agent.tools import register_tab_tools

    calls: list[list[str]] = []

    async def fake_impl(session, urls, url_contains, clipboard, progress=None):
        calls.append(list(urls))
        return [
            {
                "url": u,
                "title": "t",
                "text": "body " * 100,
                "jsonld": None,
                "links": [],
                "link_text": "Senior Engineer, Platform Infrastructure" if verbose else "",
            }
            for u in urls
        ]

    monkeypatch.setattr(tools_mod, "_read_pages_impl", fake_impl)
    monkeypatch.setattr(tools_mod, "_READ_PAGES_MAX", cap)
    prefix = "https://careers.example.com/jobs/engineering/listing" if verbose else "https://x.com"
    urls = [f"{prefix}/{i}" for i in range(count)]
    clipboard: dict = {"found_links": list(urls)}
    tools = Tools()
    register_tab_tools(tools, object(), clipboard, _items_store(), None)
    return tools, tools.registry.registry.actions["read_pages"], clipboard, calls, urls


async def test_read_pages_drained_queue_does_not_restart_from_the_top(monkeypatch) -> None:
    """An empty queue means DRAINED, not 'no queue'. Reading truthiness sent the
    fallback back to the full saved set and re-read batch one for ever."""
    _tools, entry, _clip, calls, urls = _queue_fixture(monkeypatch)

    await entry.function(browser_session=object(), file_system=_FakeFileSystem())
    await entry.function(browser_session=object(), file_system=_FakeFileSystem())
    assert calls == [urls[:3], urls[3:]]

    third = await entry.function(browser_session=object(), file_system=_FakeFileSystem())
    assert calls == [urls[:3], urls[3:]], "a drained queue must not re-read anything"
    assert "nothing left to read" in _seen_by_model(third)


async def test_read_pages_explicit_urls_do_not_clobber_the_queue(monkeypatch) -> None:
    """The completeness gate tells the agent to call read_pages([...]); that errand
    must not wipe the resume point of the main crawl."""
    _tools, entry, clipboard, calls, urls = _queue_fixture(monkeypatch)

    await entry.function(browser_session=object(), file_system=_FakeFileSystem())
    assert clipboard["_unread_links"] == urls[3:]

    await entry.function(
        browser_session=object(),
        file_system=_FakeFileSystem(),
        urls=["https://other.com/z"],
    )
    assert clipboard["_unread_links"] == urls[3:], "side errand must leave the queue alone"

    await entry.function(browser_session=object(), file_system=_FakeFileSystem())
    assert calls[-1] == urls[3:], "the crawl must resume where it stopped"


async def test_read_pages_accumulates_batches_instead_of_overwriting(monkeypatch) -> None:
    """pages.json is rewritten, not appended, so batching had to carry earlier batches
    forward — the result text tells the agent pages.json holds them all."""
    _tools, entry, _clip, _calls, urls = _queue_fixture(monkeypatch)
    fs = _FakeFileSystem()

    await entry.function(browser_session=object(), file_system=fs)
    await entry.function(browser_session=object(), file_system=fs)

    import json as _json

    saved = _json.loads(fs.files["pages.json"])
    assert [p["url"] for p in saved] == urls, "every batch must survive in pages.json"


async def test_read_pages_keeps_its_next_step_instruction_on_a_full_batch(monkeypatch) -> None:
    """The per-page detail is data and goes to a file; the instruction is the note and
    must arrive whole however big the batch is."""
    tools, _entry, _clip, _calls, _urls = _queue_fixture(
        monkeypatch, count=48, cap=48, verbose=True
    )
    entry = tools.registry.registry.actions["read_pages"]

    fs = _FakeFileSystem()
    envelope = _delivered(
        await entry.function(browser_session=object(), file_system=fs)
    )

    note = envelope["note"]
    assert "add_items_from_file" in note or "run_code_file" in note
    assert "pages.json" in note
    assert envelope["file"] in fs.files


async def test_guard_readout_files_are_not_reused_between_outputs() -> None:
    """A back-reference pins the filename for the rest of the run, so a reused name
    later serves one call's content under another call's name."""
    from openbrowse.agent.tools import register_output_guard_overrides

    payloads = ["A" * 20000, "B" * 20000, "A" * 20000]
    calls = {"n": 0}

    async def fake(params=None, **kwargs):
        text = payloads[min(calls["n"], len(payloads) - 1)]
        calls["n"] += 1
        return ActionResult(long_term_memory=text)

    tools = Tools()
    entry = tools.registry.registry.actions.get("evaluate")
    if entry is None:
        pytest.skip("browser-use build has no evaluate action")
    entry.function = fake
    register_output_guard_overrides(tools)

    fs = _FakeFileSystem()
    first = _seen_by_model(await entry.function(params=None, file_system=fs))
    second = _seen_by_model(await entry.function(params=None, file_system=fs))
    third = _seen_by_model(await entry.function(params=None, file_system=fs))

    name_a = first.split("saved to '")[1].split("'")[0]
    name_b = second.split("saved to '")[1].split("'")[0]
    assert name_a != name_b, "each spilled output needs its own file"
    assert name_a in third, "the back-reference must name the file holding THAT text"
    assert fs.files[name_a].startswith("A")


async def test_guard_breaks_a_loop_of_identical_errors() -> None:
    """An action failing identically over and over is the commonest real loop, and a
    result carrying only `error` has no model-visible fields at all."""
    from openbrowse.agent.tools import register_output_guard_overrides

    async def fake(params=None, **kwargs):
        return ActionResult(error="find_elements failed: selector not found")

    tools = Tools()
    entry = tools.registry.registry.actions.get("find_elements")
    if entry is None:
        pytest.skip("browser-use build has no find_elements action")
    entry.function = fake
    register_output_guard_overrides(tools)

    await entry.function(params=None)
    second = await entry.function(params=None)
    assert "failed with exactly the same error" in _seen_by_model(second)


async def test_guard_streaks_are_per_action_and_adjacent() -> None:
    """Two identical reads far apart are a coincidence, not a loop; saying otherwise
    pushes the agent to finish early."""
    from openbrowse.agent.tools import register_output_guard_overrides

    async def same_a(params=None, **kwargs):
        return ActionResult(long_term_memory="stable output A")

    async def other(params=None, **kwargs):
        return ActionResult(long_term_memory="something else entirely")

    from openbrowse.agent.tools import register_output_store_tools

    tools = Tools()
    register_output_store_tools(tools, _items_store(), {})
    a_entry = tools.registry.registry.actions["read_output"]
    b_entry = tools.registry.registry.actions["search_output"]
    a_entry.function = same_a
    b_entry.function = other
    register_output_guard_overrides(tools)

    await a_entry.function(params=None)
    await b_entry.function(params=None)
    interrupted = _seen_by_model(await a_entry.function(params=None))
    assert "STOP REPEATING" not in interrupted, "a different action broke the streak"

    back_to_back = _seen_by_model(await a_entry.function(params=None))
    assert "STOP REPEATING" in back_to_back


async def test_recall_is_deduped_so_repeats_do_not_grow_context() -> None:
    from openbrowse.agent.tools import register_clipboard_tools, register_output_guard_overrides

    value = "https://example.com/a-reasonably-long-url" * 40
    tools = Tools()
    register_clipboard_tools(tools, {"blob": value})
    register_output_guard_overrides(tools)
    entry = tools.registry.registry.actions["recall"]

    first = _seen_by_model(await entry.function(key="blob", file_system=_FakeFileSystem()))
    assert "example.com" in first

    second = _seen_by_model(await entry.function(key="blob", file_system=_FakeFileSystem()))
    assert "identical to earlier output" in second
    assert len(second) < len(first)


async def test_sandbox_error_field_is_bounded(tmp_path) -> None:
    result = await _run_sandbox(tmp_path, "raise ValueError('x' * 50000)")
    assert result.error
    assert len(result.error) <= 2000 + 200


# --- the delivery contract --------------------------------------------------


async def test_deliver_inlines_under_budget_and_still_writes_the_file() -> None:
    """The file exists on both routes, which is what makes a pointer never a lie and
    'give me the full object' always one read_file away."""
    import json as _json

    from openbrowse.agent.tools import deliver

    fs = _FakeFileSystem()
    payload = [{"href": "https://x.com/a", "text": "A"}]
    envelope = _delivered(
        await deliver(payload, note="found 1 link.", file_system=fs, filename="small.json")
    )

    assert envelope["data"] == payload
    assert "truncated" not in envelope
    assert _json.loads(fs.files["small.json"]) == payload


async def test_deliver_spills_over_budget_and_points_at_the_file() -> None:
    import json as _json

    from openbrowse.agent.tools import INLINE_BUDGET, POINTER_SAMPLE, deliver

    fs = _FakeFileSystem()
    payload = [{"href": f"https://example.com/page-{i}", "text": f"Item {i}"} for i in range(200)]
    envelope = _delivered(
        await deliver(payload, note="found 200 links.", file_system=fs, filename="big.json")
    )

    assert envelope["truncated"] is True
    assert envelope["total_chars"] > INLINE_BUDGET
    assert len(envelope["sample"]) <= POINTER_SAMPLE
    assert "read_file('big.json')" in envelope["read_with"]
    assert _json.loads(fs.files["big.json"]) == payload, "the file holds everything"


async def test_deliver_says_so_when_the_file_write_fails() -> None:
    from openbrowse.agent.tools import deliver

    class _BrokenFS(_FakeFileSystem):
        async def write_file(self, name: str, content: str) -> None:
            raise OSError("disk full")

    envelope = _delivered(
        await deliver(
            [{"n": i} for i in range(500)],
            note="found lots.",
            file_system=_BrokenFS(),
            filename="nope.json",
        )
    )
    assert "file" not in envelope
    assert "FAILED" in envelope["read_with"]


async def test_deliver_survives_a_broken_formatter() -> None:
    """A formatter that raises must never cost the agent its data."""
    from openbrowse.agent.tools import deliver

    def explode(_payload):
        raise ValueError("cannot render")

    envelope = _delivered(
        await deliver(
            [{"a": 1}],
            note="got one row.",
            file_system=_FakeFileSystem(),
            filename="x.json",
            formatter=explode,
        )
    )
    assert envelope["data"] == [{"a": 1}]
    assert "could not render" in envelope["note"]


async def test_deliver_always_sets_both_fields_identically() -> None:
    from openbrowse.agent.tools import deliver

    for payload in ([{"a": 1}], [{"n": i} for i in range(500)]):
        result = await deliver(
            payload, note="n.", file_system=_FakeFileSystem(), filename="f.json"
        )
        assert result.extracted_content == result.long_term_memory
        assert result.extracted_content


async def test_the_route_is_chosen_by_size_not_by_which_tool_it_is(monkeypatch) -> None:
    """find_links used to withhold its array however small it was. Five links inline,
    two hundred spill, same tool."""
    import openbrowse.agent.tools as tools_mod

    async def links_for(n):
        from openbrowse.agent.tools import deliver

        return _delivered(
            await deliver(
                [{"href": f"https://example.com/{i}", "text": f"Item {i}"} for i in range(n)],
                note=f"find_links found {n} link(s).",
                file_system=_FakeFileSystem(),
                filename="found_links.json",
            )
        )

    assert "data" in await links_for(5), "a small result must come back inline"
    assert (await links_for(200)).get("truncated") is True
    assert tools_mod.INLINE_BUDGET == 2000


def test_blank_narrative_is_detected_but_a_written_one_is_not() -> None:
    from types import SimpleNamespace

    from openbrowse.agent.runner import _has_no_narrative

    blank = SimpleNamespace(
        evaluation_previous_goal="", memory="   ", next_goal="",
        what_i_see="", plan_to_goal=None, next_move="",
    )
    assert _has_no_narrative(blank)

    written = SimpleNamespace(
        evaluation_previous_goal="", memory="", next_goal="",
        what_i_see="", plan_to_goal=None, next_move="get the hrefs",
    )
    assert not _has_no_narrative(written)
    assert not _has_no_narrative(None)


async def test_a_blank_narrative_reply_is_retried_then_accepted() -> None:
    from types import SimpleNamespace

    from openbrowse.agent.runner import _invoke_with_action_repair

    calls = {"n": 0}

    def blank_completion():
        return SimpleNamespace(
            completion=SimpleNamespace(
                evaluation_previous_goal="", memory="", next_goal="",
                what_i_see="", plan_to_goal="", next_move="",
            )
        )

    async def always_blank(msgs):
        calls["n"] += 1
        return blank_completion()

    result = await _invoke_with_action_repair(always_blank, [], object())
    assert calls["n"] == 3, "two corrective retries, then accept rather than kill the run"
    assert result is not None


# --- regressions from the high-effort review of the delivery contract -------


async def test_appended_notes_keep_the_reply_parseable() -> None:
    """Appending after the envelope's closing brace would stop the reply parsing,
    which is the one property the envelope exists to provide."""
    import json as _json

    from openbrowse.agent.tools import amend_note, deliver

    result = await deliver(
        [{"a": 1}], note="found one.", file_system=_FakeFileSystem(), filename="a.json"
    )
    amend_note(result, " STOP REPEATING THIS: same result twice.")

    envelope = _json.loads(_seen_by_model(result))
    assert "STOP REPEATING" in envelope["note"]
    assert envelope["data"] == [{"a": 1}]


def test_amend_note_falls_back_for_non_envelope_results() -> None:
    from openbrowse.agent.tools import amend_note

    plain = ActionResult(extracted_content="just a note")
    amend_note(plain, " and more.")
    assert plain.extracted_content == "just a note and more."


async def test_upstream_query_falls_back_when_the_builder_is_missing() -> None:
    """The guard was dead code: building the JS at the call site raised before the
    None check could fall back."""
    from openbrowse.agent.tools import _run_upstream_query

    assert await _run_upstream_query(object(), None, selector="a") is None
    assert await _run_upstream_query(None, lambda **kw: "js") is None


async def test_side_errand_does_not_claim_the_queue_is_empty(monkeypatch) -> None:
    """An explicit-urls call never touches the queue, so saying 'nothing is queued'
    would stop the crawl with links still pending."""
    _tools, entry, clipboard, _calls, urls = _queue_fixture(monkeypatch)

    await entry.function(browser_session=object(), file_system=_FakeFileSystem())
    assert clipboard["_unread_links"] == urls[3:]

    errand = await entry.function(
        browser_session=object(),
        file_system=_FakeFileSystem(),
        urls=["https://other.com/z"],
    )
    note = _delivered(errand)["note"]
    assert "Nothing is queued" not in note
    assert "still queued" in note


async def test_explicit_call_does_not_promise_a_resume_it_cannot_do(monkeypatch) -> None:
    _tools, entry, _clip, _calls, _urls = _queue_fixture(monkeypatch, count=5, cap=3)

    over = await entry.function(
        browser_session=object(),
        file_system=_FakeFileSystem(),
        urls=[f"https://given.com/{i}" for i in range(5)],
    )
    note = _delivered(over)["note"]
    assert "pass those remaining URLs explicitly" in note
    assert "resumes from the queue by itself" not in note


async def test_a_new_find_links_clears_the_old_resume_queue(monkeypatch) -> None:
    """A drained queue from page 1 made read_pages() skip page 2's links entirely."""
    import openbrowse.agent.tools as tools_mod

    _tools, entry, clipboard, calls, urls = _queue_fixture(monkeypatch)

    await entry.function(browser_session=object(), file_system=_FakeFileSystem())
    await entry.function(browser_session=object(), file_system=_FakeFileSystem())
    assert clipboard["_unread_links"] == []

    # page 2: a fresh find_links replaces the link set
    clipboard.pop(tools_mod._UNREAD_LINKS_KEY, None)
    clipboard["found_links"] = ["https://x.com/new-1", "https://x.com/new-2"]

    await entry.function(browser_session=object(), file_system=_FakeFileSystem())
    assert calls[-1] == ["https://x.com/new-1", "https://x.com/new-2"]


async def test_repeat_breaker_does_not_fire_on_two_different_scripts() -> None:
    """Two different scripts that both print nothing render identically; only the
    call's own arguments tell them apart."""
    from types import SimpleNamespace

    from openbrowse.agent.tools import register_output_guard_overrides

    async def silent(params=None, **kwargs):
        return ActionResult(extracted_content="(no output)\nNo files were saved.")

    from openbrowse.agent.tools import register_code_tools

    tools = Tools()
    register_code_tools(tools)
    entry = tools.registry.registry.actions["run_code_file"]
    entry.function = silent
    register_output_guard_overrides(tools)

    first = _seen_by_model(await entry.function(params=SimpleNamespace(name="one.py")))
    second = _seen_by_model(await entry.function(params=SimpleNamespace(name="two.py")))
    assert "STOP REPEATING" not in first
    assert "STOP REPEATING" not in second, "different scripts are not a loop"

    third = _seen_by_model(await entry.function(params=SimpleNamespace(name="two.py")))
    assert "STOP REPEATING" in third, "the same script twice still is"


async def test_http_fetch_file_holds_the_raw_body_its_name_promises() -> None:
    """The file's extension comes from the body's content type, so it must hold the
    body — a script doing read_json(...)['results'] got the envelope instead."""
    from openbrowse.agent.tools import deliver

    fs = _FakeFileSystem()
    raw = '{"results": [1, 2, 3]}'
    await deliver(
        {"status_code": 200, "body": raw, "headers": {}},
        note="fetched.",
        file_system=fs,
        filename="fetch_api_example_com.json",
        file_content=raw,
    )
    import json as _json

    assert _json.loads(fs.files["fetch_api_example_com.json"])["results"] == [1, 2, 3]


async def test_a_large_fetch_sample_shows_body_not_headers() -> None:
    from openbrowse.agent.tools import deliver

    body = "CONTENT-" * 2000
    envelope = _delivered(
        await deliver(
            {"status_code": 200, "body": body, "headers": {"x-" + "k" * 80: "v" * 80}},
            note="fetched.",
            file_system=_FakeFileSystem(),
            filename="page.html",
            file_content=body,
        )
    )
    assert envelope["truncated"] is True
    assert "CONTENT-" in envelope["sample"], "the sample must show actual body"


async def test_narrative_retries_do_not_consume_the_action_repair_budget() -> None:
    """A prose-less reply must not cost a later mis-typed action its last chance —
    that path abandons the step outright."""
    from types import SimpleNamespace

    from openbrowse.agent.runner import _invoke_with_action_repair

    blank = SimpleNamespace(
        completion=SimpleNamespace(
            evaluation_previous_goal="", memory="", next_goal="",
            what_i_see="", plan_to_goal="", next_move="",
        )
    )
    good = SimpleNamespace(
        completion=SimpleNamespace(
            evaluation_previous_goal="", memory="", next_goal="carry on",
            what_i_see="", plan_to_goal="", next_move="",
        )
    )
    seq = [blank, blank, "boom", "boom", good]
    calls = {"n": 0}

    async def flaky(msgs):
        item = seq[calls["n"]]
        calls["n"] += 1
        if item == "boom":
            raise ValueError(
                "1 validation error for AgentOutput\naction.0.read_pages.urls: "
                "Input should be a valid list"
            )
        return item

    result = await _invoke_with_action_repair(flaky, [], object())
    assert result is good, "two blank replies must leave the repair budget intact"
    assert calls["n"] == 5


# --- the wise.com partial-success failure -----------------------------------


def _social_store():
    """The shape that broke: rows where the URL IS the datum and there is no detail
    page to open."""
    from openbrowse.agent.output_store import OutputStore
    from openbrowse.agent.schema import json_schema_to_pydantic

    schema = {
        "type": "object",
        "properties": {
            "socialLinks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "platform": {"type": "string"},
                        "url": {"type": "string"},
                    },
                },
            }
        },
    }
    return OutputStore(json_schema_to_pydantic(schema, "S"))


async def test_social_links_are_not_throttled_as_list_row_stubs() -> None:
    """wise.com returned 2 of 5 social links: every {platform, url} row looked like a
    list-row stub, so the third onwards was refused and the run self-reported failure."""
    from openbrowse.agent.tools import register_output_store_tools

    tools = Tools()
    store = _social_store()
    clipboard: dict = {}
    register_output_store_tools(tools, store, clipboard)
    add = tools.registry.registry.actions["add_item"]

    socials = [
        ("facebook", "https://www.facebook.com/wise"),
        ("x", "https://x.com/wise"),
        ("linkedin", "https://www.linkedin.com/company/wise"),
        ("instagram", "https://www.instagram.com/wise"),
        ("youtube", "https://www.youtube.com/wise"),
    ]
    for platform, url in socials:
        result = await add.function(
            item={"platform": platform, "url": url}, file_system=_FakeFileSystem()
        )
        assert not result.error, f"{platform} was refused: {result.error}"

    assert len(store.data["socialLinks"]) == 5


async def test_a_real_list_crawl_is_still_throttled() -> None:
    """The limiter must keep working where drilling in genuinely adds something."""
    from openbrowse.agent.tools import register_output_store_tools

    tools = Tools()
    store = _items_store()
    register_output_store_tools(tools, store, {})
    add = tools.registry.registry.actions["add_item"]

    errors = []
    for i in range(4):
        result = await add.function(
            item={"title": f"Job {i}", "sourceUrl": f"https://jobs.example.com/{i}"},
            file_system=_FakeFileSystem(),
        )
        errors.append(bool(result.error))
    assert errors == [False, False, True, True], "stubs past the allowance still blocked"


def test_item_detail_field_tells_the_two_schemas_apart() -> None:
    from openbrowse.agent.tools import _item_detail_field

    assert _item_detail_field(_items_store()) == "description"
    assert _item_detail_field(_social_store()) is None


async def test_the_gate_names_items_it_refused_and_forgets_ones_that_landed() -> None:
    """Only items the agent itself proposed are surfaced — page links are not evidence
    of records, and nagging about them pushes rubbish into the output."""
    from openbrowse.agent.tools import _gate_refused_items, register_output_store_tools

    tools = Tools()
    store = _items_store()
    clipboard: dict = {}
    register_output_store_tools(tools, store, clipboard)
    add = tools.registry.registry.actions["add_item"]

    for i in range(4):
        await add.function(
            item={"title": f"Job {i}", "sourceUrl": f"https://jobs.example.com/{i}"},
            file_system=_FakeFileSystem(),
        )

    bounce = _gate_refused_items(clipboard)
    assert bounce is not None
    assert bounce.startswith("2 item(s) you tried to add were refused")
    assert bounce.rstrip().endswith("jobs.example.com/3")
    assert "jobs.example.com/2" in bounce

    # the agent goes back and adds one properly, with the detail it was told to fetch
    clipboard.setdefault("_visited", set()).add(
        __import__("openbrowse.agent.tools", fromlist=["_norm_url"])._norm_url(
            "https://jobs.example.com/2"
        )
    )
    await add.function(
        item={"title": "Job 2", "sourceUrl": "https://jobs.example.com/2"},
        file_system=_FakeFileSystem(),
    )
    remaining = _gate_refused_items(clipboard)
    assert "jobs.example.com/2" not in (remaining or "")


def test_the_gate_never_invents_candidates_from_page_links() -> None:
    """The compromise: no refusals recorded means no bounce, however many links the
    page happened to have."""
    from openbrowse.agent.tools import _gate_refused_items

    assert _gate_refused_items({}) is None
    assert _gate_refused_items({"found_links": [f"https://x.com/{i}" for i in range(30)]}) is None


# --- mark_absent as an escape from the completeness gate ---------------------


async def test_mark_absent_is_refused_until_the_agent_looks_again() -> None:
    """The wise sequence: gate bounces, agent marks everything absent without reading
    anything, gate passes on a page whose own title is declared missing."""
    from openbrowse.agent.tools import note_read_action, register_output_store_tools

    tools = Tools()
    store = _items_store()
    clipboard: dict = {"_reads_done": 3}  # it had already read earlier in the run
    register_output_store_tools(tools, store, clipboard)
    absent = tools.registry.registry.actions["mark_absent"]

    # the gate bounces and notes where the read count stood
    clipboard["_gate_bounce_reads"] = clipboard["_reads_done"]

    refused = await absent.function(field="description", reason="not published anywhere")
    assert refused.error
    assert "have not read anything since" in refused.error
    assert "description" not in store.absent_fields

    # the agent does something about it
    note_read_action(clipboard, "evaluate")

    allowed = await absent.function(field="description", reason="checked the page body")
    assert not allowed.error
    assert "description" in store.absent_fields


async def test_mark_absent_is_free_before_any_gate_bounce() -> None:
    """Settling a field you already know is unpublished is normal use, and the rule
    only applies once the gate has actually asked."""
    from openbrowse.agent.tools import register_output_store_tools

    tools = Tools()
    store = _items_store()
    register_output_store_tools(tools, store, {})
    absent = tools.registry.registry.actions["mark_absent"]

    result = await absent.function(field="description", reason="no detail pages exist")
    assert not result.error
    assert "description" in store.absent_fields


def test_only_looking_actions_count_as_looking() -> None:
    from openbrowse.agent.tools import note_read_action

    clipboard: dict = {}
    for action in ("set_field", "mark_absent", "add_item", "done", "remember"):
        note_read_action(clipboard, action)
    assert clipboard.get("_reads_done") is None, "writing to the output is not looking"

    for action in ("evaluate", "read_file", "find_links", "scroll", "read_pages"):
        note_read_action(clipboard, action)
    assert clipboard["_reads_done"] == 5


def test_absence_needs_a_look_message_names_the_tools() -> None:
    from openbrowse.agent.tools import _absent_needs_a_look

    assert _absent_needs_a_look({}) is None
    assert _absent_needs_a_look({"_gate_bounce_reads": 2, "_reads_done": 3}) is None
    msg = _absent_needs_a_look({"_gate_bounce_reads": 2, "_reads_done": 2})
    assert msg is not None
    for tool in ("evaluate", "read_pages", "read_file"):
        assert tool in msg


async def test_find_links_bare_call_collects_all_links(monkeypatch) -> None:
    # A bare call means "all links" — requiring a selector only taught models
    # to retry with href_regex='.+' after one wasted step logged as a
    # recovered "transient error".
    import openbrowse.agent.tools as tools_mod
    from browser_use import Tools

    link_map = {
        1: _link_node(1, "https://x.com/detail/1.html"),
        2: _link_node(2, "https://x.com/detail/2.html"),
        3: _link_node(3, "https://x.com/about.html"),
    }

    async def fake_settle(session, frame):
        return None

    async def fake_eval(session, js):
        return "https://x.com/list"

    monkeypatch.setattr(tools_mod, "_settle_lazy_links", fake_settle)
    monkeypatch.setattr(tools_mod, "_eval_js", fake_eval)

    class FakeSession:
        async def get_browser_state_summary(self, include_screenshot=False):
            return None

        async def get_selector_map(self):
            return link_map

        async def get_element_by_index(self, index):
            return None

    tools = Tools()
    tools_mod.register_tab_tools(tools, object(), {}, None, None)
    entry = tools.registry.registry.actions["find_links"]
    result = await entry.function(
        browser_session=FakeSession(), file_system=_FakeFileSystem()
    )
    assert not result.error
    import json as _json

    found = _json.loads(result.extracted_content)["data"]
    assert {l["href"] for l in found} == {
        "https://x.com/detail/1.html",
        "https://x.com/detail/2.html",
        "https://x.com/about.html",
    }
