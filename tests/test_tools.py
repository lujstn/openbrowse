"""Tool registration tests (no live API calls)."""

from unittest.mock import AsyncMock, patch

import pytest
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


def test_cookie_header_scoped_to_host() -> None:
    from app.agent.tools import _cookie_header_for

    jar = [
        {"name": "NID", "value": "1", "domain": ".google.com"},
        {"name": "SID", "value": "2", "domain": "www.google.com"},
        {"name": "other", "value": "3", "domain": ".example.com"},
        {"name": "", "value": "4", "domain": ".google.com"},
    ]
    header = _cookie_header_for(jar, "www.google.com")
    assert "NID=1" in header
    assert "SID=2" in header
    assert "other" not in header
    assert _cookie_header_for(jar, "") == ""


class _FakeCaptchaBrowser:
    """A browser session standing in for a page showing a Google interstitial."""

    def __init__(self, cookies: list[dict] | None = None) -> None:
        self.cookies = cookies or []
        self.submitted = False

    async def _cdp_get_cookies(self) -> list[dict]:
        return self.cookies


def _fake_eval(session: _FakeCaptchaBrowser, probe: dict | None, url: str):
    async def _eval(browser_session, expression: str):
        if "interstitial" in expression:
            return probe
        if "location.href" in expression:
            return url
        if "captcha-form" in expression:
            session.submitted = True
            return True
        return None

    return _eval


_INTERSTITIAL_PROBE = {
    "kind": "recaptcha_v2",
    "siteKey": "6LeSITE",
    "dataS": "one-shot-blob",
    "interstitial": True,
    "invisible": False,
}


def _solve_action(tools: Tools):
    return tools.registry.registry.actions["solve_captcha"].function


@pytest.mark.asyncio
async def test_solve_captcha_sends_cookies_and_data_s() -> None:
    from app.agent import tools as tools_mod

    session = _FakeCaptchaBrowser([{"name": "NID", "value": "abc", "domain": ".google.com"}])
    seen: dict = {}

    async def fake_create(client, payload):
        seen.update(payload)
        return {"errorId": 0, "solution": {"gRecaptchaResponse": "tok"}, "cost": "0.0008"}

    with patch("app.agent.tools.settings") as st:
        st.capsolver_api_key = "test-key"
        tools = Tools()
        tools_mod.register_capsolver_tool(tools)
        with (
            patch.object(tools_mod, "_eval_js", _fake_eval(session, _INTERSTITIAL_PROBE, "https://www.google.com/sorry/index")),
            patch.object(tools_mod, "_create_capsolver_task", fake_create),
            patch.object(tools_mod, "_interstitial_cleared", AsyncMock(return_value=True)),
        ):
            await _solve_action(tools)(
                captcha_type="recaptcha_v2", browser_session=session
            )

    assert seen["websiteKey"] == "6LeSITE"
    assert seen["recaptchaDataSValue"] == "one-shot-blob"
    assert "NID=abc" in seen["cookies"]


@pytest.mark.asyncio
async def test_solve_captcha_reports_failure_when_page_still_challenges() -> None:
    from app.agent import tools as tools_mod

    session = _FakeCaptchaBrowser()

    async def fake_create(client, payload):
        return {"errorId": 0, "solution": {"gRecaptchaResponse": "tok"}, "cost": 0}

    with patch("app.agent.tools.settings") as st:
        st.capsolver_api_key = "test-key"
        tools = Tools()
        tools_mod.register_capsolver_tool(tools)
        with (
            patch.object(tools_mod, "_eval_js", _fake_eval(session, _INTERSTITIAL_PROBE, "https://www.google.com/sorry/index")),
            patch.object(tools_mod, "_create_capsolver_task", fake_create),
            patch.object(tools_mod, "_interstitial_cleared", AsyncMock(return_value=False)),
        ):
            result = await _solve_action(tools)(
                captcha_type="recaptcha_v2", browser_session=session
            )

    assert result.error
    assert "still" in result.error.lower()
    assert not result.extracted_content


@pytest.mark.asyncio
async def test_solve_captcha_does_not_claim_success_on_an_ordinary_widget() -> None:
    from app.agent import tools as tools_mod

    session = _FakeCaptchaBrowser()
    probe = dict(_INTERSTITIAL_PROBE, interstitial=False, dataS="")

    async def fake_create(client, payload):
        return {"errorId": 0, "solution": {"gRecaptchaResponse": "tok"}, "cost": 0}

    with patch("app.agent.tools.settings") as st:
        st.capsolver_api_key = "test-key"
        tools = Tools()
        tools_mod.register_capsolver_tool(tools)
        with (
            patch.object(tools_mod, "_eval_js", _fake_eval(session, probe, "https://shop.example.com/checkout")),
            patch.object(tools_mod, "_create_capsolver_task", fake_create),
        ):
            result = await _solve_action(tools)(
                captcha_type="recaptcha_v2", browser_session=session
            )

    assert not result.error
    assert "solved successfully" not in (result.extracted_content or "")
    assert "has not moved" in (result.extracted_content or "")


@pytest.mark.asyncio
async def test_solve_captcha_retries_without_optional_fields_when_refused() -> None:
    from app.agent import tools as tools_mod

    session = _FakeCaptchaBrowser([{"name": "NID", "value": "abc", "domain": ".google.com"}])
    payloads: list[dict] = []

    async def fake_create(client, payload):
        payloads.append(dict(payload))
        if "cookies" in payload or "recaptchaDataSValue" in payload:
            return {"errorId": 1, "errorDescription": "ERROR_INVALID_TASK_DATA"}
        return {"errorId": 0, "solution": {"gRecaptchaResponse": "tok"}, "cost": 0}

    with patch("app.agent.tools.settings") as st:
        st.capsolver_api_key = "test-key"
        tools = Tools()
        tools_mod.register_capsolver_tool(tools)
        with (
            patch.object(tools_mod, "_eval_js", _fake_eval(session, _INTERSTITIAL_PROBE, "https://www.google.com/sorry/index")),
            patch.object(tools_mod, "_create_capsolver_task", fake_create),
            patch.object(tools_mod, "_interstitial_cleared", AsyncMock(return_value=True)),
        ):
            result = await _solve_action(tools)(
                captcha_type="recaptcha_v2", browser_session=session
            )

    assert len(payloads) == 2
    assert "cookies" not in payloads[1]
    assert not result.error


@pytest.mark.asyncio
async def test_interstitial_cleared_waits_for_the_page_to_move() -> None:
    from app.agent import tools as tools_mod

    urls = iter(
        [
            "https://www.google.com/sorry/index?q=1",
            "https://www.google.com/search?q=andy",
        ]
    )

    async def _eval(browser_session, expression: str):
        if "location.href" in expression:
            return next(urls, "https://www.google.com/search?q=andy")
        return None

    with patch.object(tools_mod, "_eval_js", _eval):
        cleared = await tools_mod._interstitial_cleared(
            object(), "https://www.google.com/sorry/index?q=1", timeout_s=5
        )
    assert cleared is True


@pytest.mark.asyncio
async def test_interstitial_not_cleared_when_the_challenge_persists() -> None:
    from app.agent import tools as tools_mod

    async def _eval(browser_session, expression: str):
        if "out.interstitial" in expression:
            return _INTERSTITIAL_PROBE
        if "location.href" in expression:
            return "https://www.google.com/sorry/index?q=1"
        return None

    with patch.object(tools_mod, "_eval_js", _eval):
        cleared = await tools_mod._interstitial_cleared(
            object(), "https://www.google.com/sorry/index?q=1", timeout_s=2
        )
    assert cleared is False


@pytest.mark.asyncio
async def test_a_failed_check_is_never_read_as_cleared() -> None:
    """A page mid-navigation makes Runtime.evaluate throw, which must not pass for
    an absent challenge: that is the false success this whole path exists to stop.
    """
    from app.agent import tools as tools_mod

    async def _always_throws(browser_session, expression: str):
        raise RuntimeError("Execution context was destroyed")

    with patch.object(tools_mod, "_eval_js", _always_throws):
        cleared = await tools_mod._interstitial_cleared(
            object(), "https://www.google.com/sorry/index?q=1", timeout_s=2
        )
    assert cleared is False


@pytest.mark.asyncio
async def test_cookies_ignore_a_non_standard_port_on_the_host() -> None:
    from app.agent.tools import _cookie_header_for

    jar = [{"name": "sid", "value": "1", "domain": "localhost"}]
    assert _cookie_header_for(jar, "localhost:8420") == "sid=1"


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
            _json.dumps([{"@type": "JobPosting", "datePublished": "2026-08-04"}]),
        ]
    )
    assert posting["datePublished"] == "2026-08-04"


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
    assert "already settled" in msg
    out = bridge["read_output"]()
    assert isinstance(out, dict)
    assert out["items"][0]["title"] == "A2"
    awaited = await bridge["read_output"]()
    assert awaited["items"][0]["title"] == "A2"
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
    from app.agent.tools import _draft_row

    store = _draft_store()
    page = {"url": "https://x.com/a", "title": "Bare page", "text": "body " * 100, "jsonld": None}
    row = _draft_row(store, page)
    assert row["sourceUrl"] == "https://x.com/a"
    assert row["title"] == "Bare page"
    assert row["description"].startswith("body")
    assert "publishedAt" not in row and "condition" not in row


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
    from app.agent.tools import _draft_row

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
    from app.agent.output_store import OutputStore
    from app.agent.schema import json_schema_to_pydantic
    from app.agent.tools import _draft_row

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
    from app.agent.tools import _strong_overlap

    assert _strong_overlap({"posted", "at"}, {"date", "posted"}) is True
    assert _strong_overlap({"location", "type"}, {"employment", "type"}) is False
    assert _strong_overlap({"location", "type"}, {"location", "type"}) is True


def test_gate_settles_partial_fields_when_all_pages_read() -> None:
    from app.agent.tools import _gate_empty_fields

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
    from app.agent.tools import _absence_unearned

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

    from app.agent.tools import register_code_tools

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
    from app.agent.tools import _load_saved_json

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
    from app.agent.tools import register_output_store_tools

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

    from app.agent.tools import register_output_store_tools

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
    clipboard: dict = {"_visited": {"https://x.com/a"}}
    store.add_item({"title": "A", "sourceUrl": "https://x.com/a"})
    store.add_item({"title": "B", "sourceUrl": "https://x.com/b"})

    assert _absence_unearned(store, clipboard, "description") is not None

    clipboard["_read_failed"] = {"https://x.com/b"}
    assert _absence_unearned(store, clipboard, "description") is None


async def test_read_one_page_waits_out_loading_shell_and_jsonld(monkeypatch) -> None:
    import json as _json

    import app.agent.tools as tools_mod

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
    from app.agent.tools import _absence_unearned

    store = _items_store()
    clipboard: dict = {"_visited": {"https://x.com/a", "https://x.com/b"}}
    store.add_item({"title": "A", "sourceUrl": "https://x.com/a"})
    store.add_item({"title": "B", "sourceUrl": "https://x.com/b"})

    blocked = _absence_unearned(store, {}, "description")
    assert blocked is not None and "read_pages" in blocked

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
    from app.agent.tools import register_completeness_gate

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
    from app.agent.tools import register_completeness_gate

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
    from app.agent.output_store import OutputStore
    from app.agent.schema import json_schema_to_pydantic
    from app.agent.tools import _draft_row

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

    from app.agent.tools import register_code_tools

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
    result = await _run_sandbox(tmp_path, "x = '{\"a\": 1}'\nprint(x['a'])")
    assert result.error
    assert "parsed dicts" in result.error
    assert "read_output()" in result.error


async def test_run_code_file_shows_code_tab_and_restores_focus(
    tmp_path, monkeypatch
) -> None:
    import types

    import app.agent.tools as tools_mod

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
    from app.agent.tools import register_code_tools

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
    from app.agent.code_stream import _partial_json_string_prefix

    assert _partial_json_string_prefix('x = 1\\nprint(x)", "rest') == "x = 1\nprint(x)"
    assert _partial_json_string_prefix("half an esc\\") == "half an esc"
    assert _partial_json_string_prefix('unicode \\u00a3 sign') == "unicode £ sign"
    assert _partial_json_string_prefix('trunc \\u00') == "trunc "


async def test_code_stream_observer_announces_and_pushes(monkeypatch) -> None:
    import app.agent.tools as tools_mod
    from app.agent.code_stream import CodeStreamObserver

    spawned: list[str] = []
    focused: list[str] = []

    async def fake_spawn(session, url):
        spawned.append(url)
        return "code-tid"

    async def fake_focus(session, tid):
        focused.append(tid)

    import app.agent.code_stream as cs_mod

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
    import app.agent.tools as tools_mod
    from app.agent.code_stream import CodeStreamObserver

    async def fake_spawn(session, url):
        raise AssertionError("must not open a tab for prose mentions")

    monkeypatch.setattr(tools_mod, "_spawn_tab", fake_spawn)
    obs = CodeStreamObserver(object(), {}, None)
    await obs.on_partial(
        '{"thinking": "I could use run_code_file here, or maybe update_items instead"'
    )
    assert obs._announced is False


async def test_code_stream_settle_restores_focus_and_closes_orphans(monkeypatch) -> None:
    import app.agent.tools as tools_mod
    from app.agent.code_stream import CodeStreamObserver

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

    from app.agent.tools import _compact_json_text

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

    from app.agent.tools import register_output_guard_overrides

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
    from app.agent.tools import _saved_links_sans_offhost

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
    import app.agent.tools as tools_mod

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
    from app.agent.tools import _absence_unearned, _refresh_read_items

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
    from app.agent.tools import _gate_empty_fields, _refresh_read_items

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
    from app.agent.tools import _absence_unearned, _refresh_read_items

    store = _items_store()
    store.add_item({"title": "A", "sourceUrl": "https://x.com/a"})
    store.add_item({"title": "B", "sourceUrl": "https://x.com/never-read"})
    clipboard = {"_visited": {"https://x.com/a"}, "_read_failed": set()}
    _refresh_read_items(store, clipboard)
    store.update_item(0, {"sourceUrl": "https://ats.example.com/a"})
    msg = _absence_unearned(store, clipboard, "description")
    assert msg is not None and "have not been read" in msg


async def test_read_one_page_skips_frame_filter_on_panel_host(monkeypatch) -> None:
    import app.agent.tools as tools_mod

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
    import app.agent.tools as tools_mod

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
    import app.agent.tools as tools_mod

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
                        "applyUrl": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                        "description": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                    },
                },
            }
        },
    }
    return OutputStore(json_schema_to_pydantic(schema, "ApplyT"))


def test_draft_row_rejects_footer_text_and_fills_apply_url_from_links() -> None:
    from app.agent.tools import _draft_row

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
    from app.agent.tools import _draft_row

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
    from app.agent.tools import register_completeness_gate

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
    from app.agent.tools import register_completeness_gate

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
    from app.agent.tools import register_completeness_gate

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
    from app.agent.tools import _scan_link_map

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
    from app.agent.tools import _scan_link_map

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
    import types as _t

    import app.agent.tools as tools_mod
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
    import app.agent.tools as tools_mod
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

    found = _json.loads(result.extracted_content)
    assert len(found) == 4
    assert all("embed_jid" in l["href"] for l in found)


async def test_flag_shell_reads_falls_back_to_dom_probe(monkeypatch) -> None:
    import app.agent.tools as tools_mod

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
    import app.agent.tools as tools_mod

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
    from app.agent.tools import register_completeness_gate
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
    from app.agent.tools import register_completeness_gate
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
    from app.agent.tools import _frame_failure

    assert _frame_failure("read the embedding shell, not this page's real content")
    assert _frame_failure("no embedded panel matching 'embed' rendered")
    assert _frame_failure("page embeds its content in a panel from x.com")
    assert _frame_failure("not attempted — read_pages stopped before its time budget")
    assert not _frame_failure("HTTPError: 404")
    assert not _frame_failure("no readable text rendered")


async def test_frame_failures_do_not_unlock_mark_absent() -> None:
    from app.agent.tools import _absence_unearned

    store = _items_store()
    clipboard: dict = {"_visited": {"https://x.com/a"}}
    store.add_item({"title": "A", "sourceUrl": "https://x.com/a"})
    store.add_item({"title": "B", "sourceUrl": "https://x.com/b"})

    clipboard["_read_failed_frame"] = {"https://x.com/b"}
    refusal = _absence_unearned(store, clipboard, "description")
    assert refusal is not None and "https://x.com/b" in refusal


def _sandbox_browser_with_frames(frames):
    from app.agent.tools import _SandboxBrowser

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

    import app.agent.tools as tools_mod

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

    import app.agent.tools as tools_mod

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

    import app.agent.tools as tools_mod

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
    import app.agent.tools as tools_mod

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
    import app.agent.tools as tools_mod

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
    import app.agent.tools as tools_mod
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
    import app.agent.tools as tools_mod
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
    import app.agent.tools as tools_mod
    from app.agent.tools import register_tab_tools
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

    import app.agent.tools as tools_mod
    from app.agent.tools import TabManager

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


async def test_sandbox_evaluate_and_get_html_note_embeds(monkeypatch) -> None:
    import app.agent.tools as tools_mod
    from app.agent.tools import _SandboxBrowser

    async def fake_eval(session, js):
        if "outerHTML" in js:
            return "<div>shell</div>"
        return "thin shell text"

    async def dom_hosts(session):
        return ["board.example.com"]

    monkeypatch.setattr(tools_mod, "_eval_js", fake_eval)
    monkeypatch.setattr(tools_mod, "_dom_iframe_hosts", dom_hosts)

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
    import app.agent.tools as tools_mod

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
    import app.agent.tools as tools_mod

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
    from app.agent.tools import _flag_lone_frame_fallbacks

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
    from app.agent.tools import register_output_store_tools
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
    from app.agent.tools import _store_bridge

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
    import app.agent.tools as tools_mod
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

    found = _json.loads(result.extracted_content)
    assert len(found) == 3
    assert "WARNING" not in (result.long_term_memory or "")


async def test_find_links_warns_when_matched_frame_stays_tiny(monkeypatch) -> None:
    import app.agent.tools as tools_mod
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
    import app.agent.tools as tools_mod
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

    found = _json.loads(result.extracted_content)
    hrefs = {l["href"] for l in found}
    assert "https://x.com/jobs?src=board.example.com&jid=0" in hrefs
    assert "https://x.com/jobs?src=board.example.com&jid=1" in hrefs
    note = result.long_term_memory or ""
    assert "recovered by matching hrefs" in note
    assert clipboard["found_links_frame"] == "board.example.com"


def test_system_metrics_pressure_levels(monkeypatch) -> None:
    import app.system_metrics as sm

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

    import app.system_metrics as sm

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
    import app.agent.tools as tools_mod
    import app.system_metrics as sm

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


async def test_find_links_relaxes_starving_caller_filters(monkeypatch) -> None:
    import app.agent.tools as tools_mod
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

    found = _json.loads(result.extracted_content)
    assert len(found) == 3
    assert any("embed_jid=aaa" in l["href"] for l in found)
    note = result.long_term_memory or ""
    assert "kept 0 of 3" in note
    assert "rewrites its anchors to the host page's own URLs" in note
    assert "embed_jid" in note


async def test_gate_bounce_has_no_termination_vocabulary() -> None:
    from app.agent.tools import register_completeness_gate
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
    from app.agent.output_store import OutputStore
    from app.agent.schema import json_schema_to_pydantic
    from app.agent.tools import _draft_row, _labelled_pairs

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
    from app.agent.output_store import OutputStore
    from app.agent.schema import json_schema_to_pydantic
    from app.agent.tools import _draft_row

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

    from app.agent.output_store import OutputStore
    from app.agent.schema import json_schema_to_pydantic
    from app.agent.tools import _draft_row

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
    from app.agent.tools import _draft_row

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
    from app.agent.tools import _draft_row

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
    import app.agent.tools as tools_mod

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
    import app.agent.tools as tools_mod

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
    import app.agent.tools as tools_mod

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
    import app.agent.tools as tools_mod

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
    import app.agent.tools as tools_mod

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
    from app.agent.tools import _tolerate_json_list

    assert _tolerate_json_list('["a", "b"]') == ["a", "b"]
    assert _tolerate_json_list(["a"]) == ["a"]
    assert _tolerate_json_list("plainField") == "plainField"
    assert _tolerate_json_list("[not json") == "[not json"
    assert _tolerate_json_list(None) is None


async def test_mark_absent_accepts_json_string_list() -> None:
    """Claude's observed wire drift: a list argument serialised as its JSON
    text must settle every named field, not bounce as one unknown field."""
    from app.agent.tools import register_output_store_tools

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

    from app.agent.tools import register_search_page_flow

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

    params = SimpleNamespace(pattern="salary|equity", css_scope="null")
    first = await entry.function(params=params)
    assert calls[-1] is None
    assert "pages.json" in first.extracted_content

    params2 = SimpleNamespace(pattern="salary|equity", css_scope=None)
    second = await entry.function(params=params2)
    assert "searched 2 times" in second.extracted_content
    assert "run_code_file" in second.extracted_content


async def test_read_output_fields_json_string_normalised_at_boundary() -> None:
    """The strict signature rejects a stringified list; the boundary normaliser
    repairs it before validation, so the pair must round-trip."""
    from app.agent.leak_repair import coerce_action_param_shapes
    from app.agent.tools import action_param_kinds, register_output_store_tools

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
    from app.agent.tools import _param_kind, action_param_kinds, register_output_store_tools

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
    from app.agent.tools import register_clipboard_tools

    tools = Tools()
    clipboard = {"found_links": ["https://x.com/a"]}
    register_clipboard_tools(tools, clipboard)
    entry = tools.registry.registry.actions["remember"]
    params = entry.param_model(key="found_links", value="oops")
    result = await entry.function(params=params)
    assert result.error and "internal session key" in result.error
    assert clipboard["found_links"] == ["https://x.com/a"]


def test_saved_links_survive_corruption() -> None:
    from app.agent.tools import _saved_links_sans_offhost

    kept, skipped = _saved_links_sans_offhost({"found_links": "https://x.com/a"})
    assert kept == [] and skipped == 0
    kept, _ = _saved_links_sans_offhost(
        {"found_links": ["https://a", "https://b"], "found_links_offhost": "https"}
    )
    assert kept == ["https://a", "https://b"]


def test_filter_page_urls() -> None:
    from app.agent.tools import _filter_page_urls

    kept, dropped = _filter_page_urls(["null", "https://x.com/a", "jobs", "http://y.com"])
    assert kept == ["https://x.com/a", "http://y.com"] and dropped == 2
    kept, dropped = _filter_page_urls(None)
    assert kept is None and dropped == 0
    kept, dropped = _filter_page_urls(["null"])
    assert kept == [] and dropped == 1


def test_coerce_scalar_unwraps_json_strings_for_container_fields() -> None:
    from app.agent.output_store import _coerce_scalar

    assert _coerce_scalar('["a", "b"]', list[str]) == ["a", "b"]
    assert _coerce_scalar('{"k": "v"}', dict[str, str]) == {"k": "v"}
    assert _coerce_scalar('["a"]', str) == '["a"]'
    assert _coerce_scalar("plain", list[str]) == "plain"


async def test_gate_emits_pass_event_on_clean_done() -> None:
    from app.agent.tools import register_completeness_gate, register_output_store_tools

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
