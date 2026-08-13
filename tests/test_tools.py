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
    assert "write_code_file" not in actions
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


def test_norm_url() -> None:
    from app.agent.tools import _norm_url

    a = _norm_url("https://www.example.com/listings?embed_id=ABC#section")
    b = _norm_url("https://www.example.com/listings?embed_id=ABC/")
    c = _norm_url("HTTPS://WWW.EXAMPLE.COM/listings?embed_id=ABC")
    assert a == b == c == "https://www.example.com/listings?embed_id=abc"
    assert _norm_url("") == ""


def test_bare_stub_count_counts_unopened_contentless_items() -> None:
    from app.agent.tools import _norm_url, _bare_stub_count

    store = _items_store()
    store.add_item({"title": "A", "sourceUrl": "https://x/a"})
    store.add_item({"title": "B", "sourceUrl": "https://x/b"})
    store.add_item({"title": "C"})

    assert _bare_stub_count(store, set()) == 2
    assert _bare_stub_count(store, {_norm_url("https://x/a")}) == 1
    assert _bare_stub_count(store, {_norm_url("https://x/a"), _norm_url("https://x/b")}) == 0


def test_item_with_description_is_not_a_stub() -> None:
    from app.agent.tools import _bare_stub_count, _is_bare_stub

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
    from app.agent.output_store import OutputStore
    from app.agent.schema import json_schema_to_pydantic
    from app.agent.tools import _bare_stub_count

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
    from app.agent.output_store import OutputStore
    from app.agent.schema import json_schema_to_pydantic

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
    from app.agent.tools import register_output_store_tools

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
    from app.agent.tools import TabManager, register_tab_tools

    tools = Tools()
    register_tab_tools(tools, TabManager(session=None), {})
    actions = tools.registry.registry.actions
    assert "read_pages" in actions
    assert "open_tabs" in actions


def test_parse_jsonld_blobs() -> None:
    import json as _json

    from app.agent.tools import _parse_jsonld_blobs

    assert _parse_jsonld_blobs(None) is None
    assert _parse_jsonld_blobs([]) is None
    assert _parse_jsonld_blobs(["{not json"]) is None
    plain = _parse_jsonld_blobs([_json.dumps({"@type": "Organization", "name": "X"})])
    assert plain["name"] == "X"
    posting = _parse_jsonld_blobs(
        [
            _json.dumps({"@type": "Organization"}),
            _json.dumps([{"@type": "JobPosting", "datePosted": "2026-08-04"}]),
        ]
    )
    assert posting["datePosted"] == "2026-08-04"


def test_stub_block_msg_throttles_unvisited_listing_items() -> None:
    from app.agent.tools import _MAX_UNVISITED_STUBS, _stub_block_msg

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

    from app.agent.tools import _store_bridge

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
    assert "Marked 'description'" in msg
    assert "A2" in bridge["read_output"]()
    assert bridge["coverage"]().startswith("Coverage — ")


async def test_store_bridge_respects_stub_limit() -> None:
    from app.agent.tools import _MAX_UNVISITED_STUBS, _store_bridge

    store = _items_store()
    fs = _FakeFileSystem()
    bridge = _store_bridge(store, {}, fs)
    for i in range(_MAX_UNVISITED_STUBS):
        await bridge["add_item"]({"title": f"J{i}", "sourceUrl": f"https://x.com/{i}"})
    msg = await bridge["add_item"]({"title": "J9", "sourceUrl": "https://x.com/9"})
    assert "Slow down" in msg
    assert store.item_count() == _MAX_UNVISITED_STUBS


async def test_read_pages_impl_waves_retry_and_visited(monkeypatch) -> None:
    import app.agent.tools as tools_mod

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
        session, url, tid, url_contains, claimed, baseline, allow_sole_candidate=False
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
    import app.agent.tools as tools_mod

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
        session, url, tid, url_contains, claimed, baseline, allow_sole_candidate=False
    ):
        reads.append(url)
        if url == dead:
            return {"url": url, "error": "no embedded panel matching 'embed' rendered"}
        jsonld = {"datePosted": "2026-08-04"}
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
    assert by_url[slow_ld]["jsonld"] == {"datePosted": "2026-08-04"}
    assert tools_mod._norm_url(dead) in clipboard["_read_failed"]
    assert tools_mod._norm_url(slow_ld) in clipboard["_visited"]


def _draft_store():
    from app.agent.output_store import OutputStore
    from app.agent.schema import json_schema_to_pydantic

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
                        "postedAt": {"anyOf": [{"type": "string"}, {"type": "null"}]},
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
    from app.agent.tools import _draft_row

    store = _draft_store()
    page = {
        "url": "https://x.com/listings?id=abc12345",
        "title": "Widget One | Store",
        "text": "t" * 500,
        "jsonld": {
            "@type": "Product",
            "title": "Widget One",
            "description": "<p>Great &amp; sturdy.</p><p>Second para.</p>",
            "datePosted": "2026-08-04",
            "employmentType": "FULL_TIME",
            "validThrough": "2026-12-01",
        },
    }
    row = _draft_row(store, page)
    assert row["sourceUrl"] == "https://x.com/listings?id=abc12345"
    assert row["title"] == "Widget One"
    assert row["postedAt"] == "2026-08-04"
    assert "Great & sturdy." in row["description"]
    assert "<p>" not in row["description"]
    assert "sellerDescription" not in row
    extra_keys = {e["key"] for e in row.get("extra") or []}
    assert "validThrough" in extra_keys
    assert "condition" not in row

    ok, msg = store.add_item(row)
    assert ok is True, msg


def test_draft_row_falls_back_to_page_text_and_invents_nothing() -> None:
    from app.agent.tools import _draft_row

    store = _draft_store()
    page = {"url": "https://x.com/a", "title": "Bare page", "text": "body " * 100, "jsonld": None}
    row = _draft_row(store, page)
    assert row["sourceUrl"] == "https://x.com/a"
    assert row["title"] == "Bare page"
    assert row["description"].startswith("body")
    assert "postedAt" not in row and "condition" not in row


def test_strip_html_preserves_paragraphs() -> None:
    from app.agent.tools import _strip_html

    out = _strip_html("<p>One</p><p>Two &amp; three</p><br>Four")
    assert "One" in out and "Two & three" in out
    assert "<" not in out
    assert "\n" in out


def test_awaitable_helpers_work_with_and_without_await() -> None:
    import asyncio

    from app.agent.tools import _AwaitableStr, _awaitable

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

    from app.agent.tools import register_code_tools

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
    import app.agent.tools as tools_mod

    async def fake_iframe_targets(session):
        return []

    async def fake_spawn(session, url):
        return "tid"

    async def fake_close(session, tid):
        pass

    async def fake_focus(session, tid):
        pass

    async def fake_read_one(
        session, url, tid, url_contains, claimed, baseline, allow_sole_candidate=False
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

    import app.agent.tools as tools_mod

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
        session, url, tid, url_contains, claimed, baseline, allow_sole_candidate=False
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
        session, url, tid, url_contains, claimed, baseline, allow_sole_candidate=False
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
        session, url, tid, url_contains, claimed, baseline, allow_sole_candidate=False
    ):
        return {"url": url, "text": "body " * 60, "jsonld": None, "links": []}

    tools_mod, order, session = _wave_fakes(monkeypatch, read_one)
    monkeypatch.setattr(tools_mod, "_READ_PAGES_BUDGET_S", 0.0)
    urls = ["https://x.com/1", "https://x.com/2"]
    clipboard: dict = {}
    pages = await tools_mod._read_pages_impl(session, urls, None, clipboard, concurrency=2)

    assert all("not attempted" in (p.get("error") or "") for p in pages)
    assert not [ev for ev in order if ev[0] == "spawn"]
    assert {tools_mod._norm_url(u) for u in urls} <= clipboard["_read_failed"]


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
    from app.agent.tools import _url_discriminators

    tokens = _url_discriminators(
        "https://www.example.com/listings/category-x?item_id=3dee50f9-717b-4311&ref=ab"
    )
    assert "3dee50f9-717b-4311" in tokens
    assert "category-x" in tokens
    assert "ab" not in tokens
    assert _url_discriminators("") == set()


async def test_mark_absent_accepts_read_failures_as_looked() -> None:
    from app.agent.tools import _absence_unearned

    store = _items_store()
    store.add_item({"title": "A", "sourceUrl": "https://x.com/a", "description": "d" * 200})
    store.add_item({"title": "B", "sourceUrl": "https://x.com/b", "description": "d" * 200})

    clipboard: dict = {"_visited": {"https://x.com/a"}}
    assert _absence_unearned(store, clipboard, "description") is not None

    clipboard["_read_failed"] = {"https://x.com/b"}
    assert _absence_unearned(store, clipboard, "description") is None


async def test_read_one_page_waits_out_loading_shell_and_jsonld(monkeypatch) -> None:
    import json as _json

    import app.agent.tools as tools_mod

    texts = iter(["Loading", "Loading…", "X" * 300, "X" * 300, "X" * 400])
    jsonlds = iter([[], [_json.dumps({"@type": "JobPosting", "datePosted": "2026-08-04"})]])
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
        session, tid, needle, claimed, baseline, allow_sole=False, page_url=None
    ):
        return "frame-1"

    monkeypatch.setattr(tools_mod, "_eval_on_target", fake_eval)
    monkeypatch.setattr(tools_mod, "_match_frame_target", fake_match)

    page = await tools_mod._read_one_page(
        None, "https://x.com/j1", "tid-1", "embed", set(), set()
    )
    assert not page.get("error")
    assert len(page["text"]) >= 300
    assert page["jsonld"]["datePosted"] == "2026-08-04"
    assert page["frame_matched"] is True


async def test_mark_absent_requires_pages_read() -> None:
    from app.agent.tools import _absence_unearned

    store = _items_store()
    clipboard: dict = {}
    store.add_item({"title": "A", "sourceUrl": "https://x.com/a", "description": "d" * 200})
    store.add_item({"title": "B", "sourceUrl": "https://x.com/b", "description": "d" * 200})

    blocked = _absence_unearned(store, clipboard, "description")
    assert blocked is not None and "read_pages" in blocked

    clipboard["_visited"] = {"https://x.com/a", "https://x.com/b"}
    assert _absence_unearned(store, clipboard, "description") is None


async def test_mark_absent_earn_check_skips_top_level_and_urlless() -> None:
    from app.agent.output_store import OutputStore
    from app.agent.schema import json_schema_to_pydantic
    from app.agent.tools import _absence_unearned

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
    from app.agent.output_store import OutputStore
    from app.agent.schema import json_schema_to_pydantic

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
                        "postedAt": {"anyOf": [{"type": "string"}, {"type": "null"}]},
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
    from app.agent.tools import register_completeness_gate

    tools = Tools()
    store = _hints_store()
    store.add_item(
        {"title": "A", "extra": [{"key": "datePosted", "value": "2026-08-04"}]}
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
    assert "datePosted" in first.extracted_content
    assert "postedAt" in first.extracted_content
    assert len(bounces) == 1

    second = await entry.function(params=params, file_system=fs)
    assert second.is_done is True
    assert len(bounces) == 1


async def test_completeness_gate_passes_when_absent_marked() -> None:
    from app.agent.tools import register_completeness_gate

    tools = Tools()
    store = _hints_store()
    store.add_item({"title": "A"})
    store.mark_absent("postedAt", "no dates published")
    store.mark_absent("extra", "no extra attributes shown")
    register_completeness_gate(tools, store, None)
    entry = tools.registry.registry.actions["done"]
    params = entry.param_model(text="all done", success=True)

    result = await entry.function(params=params, file_system=_FakeFileSystem())
    assert result.is_done is True
