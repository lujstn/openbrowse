"""Tests for dashboard Basic auth, run form, and API fail-closed behaviour."""

import asyncio
import base64
from dataclasses import replace
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import settings
from app.db.models import init_db
from app.main import app


@pytest.fixture(autouse=True)
async def setup(tmp_path, monkeypatch):
    test_settings = replace(
        settings,
        db_path=tmp_path / "test.db",
        data_dir=tmp_path / "data",
        profiles_dir=tmp_path / "data" / "profiles",
        api_key="secret-key",
        dashboard_user="admin",
        dashboard_password="",
        allow_insecure_no_auth=False,
    )
    monkeypatch.setattr("app.config.settings", test_settings)
    monkeypatch.setattr("app.db.models.settings", test_settings)
    monkeypatch.setattr("app.auth.settings", test_settings)
    monkeypatch.setattr("app.dashboard.routes.settings", test_settings)
    monkeypatch.setattr("app.profiles.storage.settings", test_settings)
    (tmp_path / "data" / "profiles").mkdir(parents=True)
    await init_db()


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _basic(user: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


async def test_dashboard_requires_auth(client):
    resp = await client.get("/")
    assert resp.status_code == 401


async def test_dashboard_rejects_wrong_password(client):
    resp = await client.get("/", headers=_basic("admin", "wrong"))
    assert resp.status_code == 401


async def test_dashboard_accepts_valid_auth(client):
    resp = await client.get("/", headers=_basic("admin", "secret-key"))
    assert resp.status_code == 200


async def test_run_page_serves_form(client):
    resp = await client.get("/", headers=_basic("admin", "secret-key"))
    assert resp.status_code == 200
    assert "<textarea" in resp.text
    assert 'name="task"' in resp.text


async def test_sessions_page_still_serves(client):
    resp = await client.get("/sessions", headers=_basic("admin", "secret-key"))
    assert resp.status_code == 200


@patch("app.dashboard.routes.pool.submit", new_callable=AsyncMock)
async def test_run_creates_and_dispatches(mock_submit, client):
    import asyncio

    from app.dashboard import routes

    resp = await client.post(
        "/run",
        data={"task": "Go to example.com", "model": "claude-sonnet-5"},
        headers=_basic("admin", "secret-key"),
    )
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/session/")
    mock_submit.assert_called_once()
    if routes._dispatched_tasks:
        await asyncio.gather(*routes._dispatched_tasks, return_exceptions=True)


async def test_vnc_asset_requires_auth(client):
    resp = await client.get("/vnc/some-session-id/vnc.html")
    assert resp.status_code == 401


async def test_dashboard_sse_requires_auth(client):
    resp = await client.get("/sse/sessions")
    assert resp.status_code == 401


async def test_api_fails_closed_without_key(client, monkeypatch):
    closed = replace(settings, api_key="", allow_insecure_no_auth=False)
    monkeypatch.setattr("app.auth.settings", closed)
    resp = await client.post("/v3/sessions", json={"task": "x"})
    assert resp.status_code == 401


async def test_profiles_page_shows_domains_not_user_id(client):
    from app.profiles.importer import import_profile

    await import_profile(
        "pid-1",
        {"cookies": [
            {"name": "s", "value": "v", "domain": ".workatastartup.com"},
            {"name": "t", "value": "v", "domain": ".ycombinator.com"},
        ], "origins": []},
        name="YC",
    )
    resp = await client.get("/profiles", headers=_basic("admin", "secret-key"))
    assert resp.status_code == 200
    assert "Cookie Domains" in resp.text
    assert "<th>User ID</th>" not in resp.text
    assert 'name="user_id"' not in resp.text
    assert "2 domains" in resp.text


async def test_profile_create_makes_row_and_file(client, tmp_path):
    from app.db import crud

    resp = await client.post(
        "/profiles/create",
        data={"name": "Work", "user_id": "u1"},
        headers=_basic("admin", "secret-key"),
    )
    assert resp.status_code == 303
    profiles, total = await crud.list_profiles(page=1, page_size=50)
    assert total == 1
    pid = profiles[0]["id"]
    assert profiles[0]["name"] == "Work"
    assert (tmp_path / "data" / "profiles" / f"{pid}.json").exists()


async def test_profile_edit_name(client):
    from app.db import crud

    profile = await crud.create_profile(name="Old")
    resp = await client.post(
        f"/profiles/{profile['id']}/edit",
        data={"name": "New", "user_id": "", "new_id": ""},
        headers=_basic("admin", "secret-key"),
    )
    assert resp.status_code == 303
    updated = await crud.get_profile(profile["id"])
    assert updated["name"] == "New"


async def test_profile_rename_uuid_repoints_session_and_renames_file(client, tmp_path):
    from app.db import crud

    await client.post(
        "/profiles/create", data={"name": "P"}, headers=_basic("admin", "secret-key")
    )
    profiles, _ = await crud.list_profiles(page=1, page_size=50)
    pid = profiles[0]["id"]
    session = await crud.create_session(task="t", profile_id=pid)
    new_id = "renamed-profile-id"

    resp = await client.post(
        f"/profiles/{pid}/edit",
        data={"name": "P", "user_id": "", "new_id": new_id},
        headers=_basic("admin", "secret-key"),
    )
    assert resp.status_code == 303
    assert await crud.get_profile(pid) is None
    assert await crud.get_profile(new_id) is not None
    assert (await crud.get_session(session["id"]))["profile_id"] == new_id
    assert not (tmp_path / "data" / "profiles" / f"{pid}.json").exists()
    assert (tmp_path / "data" / "profiles" / f"{new_id}.json").exists()


async def test_profile_rename_rejects_path_escape(client):
    from app.db import crud

    profile = await crud.create_profile(name="P")
    resp = await client.post(
        f"/profiles/{profile['id']}/edit",
        data={"name": "P", "user_id": "", "new_id": "../evil"},
        headers=_basic("admin", "secret-key"),
    )
    assert resp.status_code == 400
    assert await crud.get_profile(profile["id"]) is not None


async def test_profile_delete_cascade_nulls_session(client, tmp_path):
    from app.db import crud

    await client.post(
        "/profiles/create", data={"name": "D"}, headers=_basic("admin", "secret-key")
    )
    profiles, _ = await crud.list_profiles(page=1, page_size=50)
    pid = profiles[0]["id"]
    session = await crud.create_session(task="t", profile_id=pid)

    resp = await client.post(
        f"/profiles/{pid}/delete", headers=_basic("admin", "secret-key")
    )
    assert resp.status_code == 303
    assert await crud.get_profile(pid) is None
    assert (await crud.get_session(session["id"]))["profile_id"] is None
    assert not (tmp_path / "data" / "profiles" / f"{pid}.json").exists()


async def test_session_log_export_scopes(client):
    import json

    from app.db import crud

    session = await crud.create_session(task="scrape listings")
    sid = session["id"]
    await crud.update_session(sid, output=json.dumps({"items": [{"title": "A"}]}))
    await crud.create_message(
        session_id=sid,
        role="ai",
        msg_type="browser_action",
        summary="step one",
        data=json.dumps({"step": 1, "see": "a page", "thinking": "private reasoning"}),
    )
    auth = _basic("admin", "secret-key")

    full = (await client.get(f"/session/{sid}/log", headers=auth)).json()
    assert any(
        m["data"] and "private reasoning" in m["data"] for m in full["messages"]
    )

    steps = (
        await client.get(f"/session/{sid}/log?scope=steps", headers=auth)
    ).json()
    step_rows = [m for m in steps["messages"] if m["data"]]
    assert step_rows and all("private reasoning" not in m["data"] for m in step_rows)
    assert any("a page" in m["data"] for m in step_rows)

    output = (
        await client.get(f"/session/{sid}/log?scope=output", headers=auth)
    ).json()
    assert output == {"items": [{"title": "A"}]}


async def test_settings_page_hides_secrets_behind_password_inputs(
    client, tmp_path, monkeypatch
):
    env = tmp_path / ".env"
    env.write_text("API_KEY=supersecret\nMAX_CONCURRENT_SESSIONS=3\n")
    monkeypatch.setattr("app.dashboard.routes._ENV_PATH", env)
    resp = await client.get("/settings", headers=_basic("admin", "secret-key"))
    assert resp.status_code == 200
    assert 'type="password"' in resp.text
    assert 'value="supersecret"' in resp.text
    assert "MAX_CONCURRENT_SESSIONS" in resp.text
    assert 'value="3"' in resp.text
    assert "DASHBOARD_USER" in resp.text and "DASHBOARD_PASSWORD" in resp.text
    auth_pos = resp.text.find("Authentication")
    providers_pos = resp.text.find("Model providers")
    assert 0 < auth_pos < providers_pos
    assert resp.text.find("DASHBOARD_USER") < providers_pos


async def test_settings_save_updates_and_removes_values(client, tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("API_KEY=supersecret\nMAX_CONCURRENT_SESSIONS=3\nOLD_VAR=x\n")
    monkeypatch.setattr("app.dashboard.routes._ENV_PATH", env)
    restarts = []
    monkeypatch.setattr(
        "app.dashboard.routes._schedule_restart", lambda: restarts.append(1)
    )
    resp = await client.post(
        "/settings",
        headers=_basic("admin", "secret-key"),
        data={
            "key": ["API_KEY", "MAX_CONCURRENT_SESSIONS", "OLD_VAR", "NEW_VAR"],
            "value": ["supersecret", "1", "", "hello"],
        },
    )
    assert resp.status_code == 200
    assert "Restarting OpenBrowse" in resp.text
    assert restarts == [1]
    text = env.read_text()
    assert "API_KEY=supersecret" in text
    assert "MAX_CONCURRENT_SESSIONS=1" in text
    assert "OLD_VAR" not in text
    assert "NEW_VAR=hello" in text


async def test_settings_requires_auth(client):
    resp = await client.get("/settings")
    assert resp.status_code == 401


async def test_static_logo_assets_served(client):
    resp = await client.get("/static/openbrowse.ico")
    assert resp.status_code == 200
    resp = await client.get("/static/openbrowse.svg")
    assert resp.status_code == 200


async def test_settings_restart_outcome_banners(client, tmp_path, monkeypatch):
    import time as _time

    env = tmp_path / ".env"
    env.write_text("API_KEY=k\n")
    monkeypatch.setattr("app.dashboard.routes._ENV_PATH", env)
    now = _time.time()
    monkeypatch.setattr("app.dashboard.routes._STARTED_AT", now)

    resp = await client.get(
        f"/settings?restarted={int(now) - 60}", headers=_basic("admin", "secret-key")
    )
    assert resp.status_code == 200
    assert "does not appear to have" not in resp.text

    resp = await client.get(
        f"/settings?restarted={int(now) + 60}", headers=_basic("admin", "secret-key")
    )
    assert "does not appear to have" in resp.text


async def test_followup_message_redispatches_idle_keepalive_session(
    client, monkeypatch
):
    from unittest.mock import AsyncMock

    from app.db import crud

    submit = AsyncMock()
    monkeypatch.setattr("app.dashboard.routes.pool.submit", submit)
    session = await crud.create_session(task="first task", keep_alive=True)
    await crud.update_session(session["id"], status="idle")

    resp = await client.post(
        f"/session/{session['id']}/message",
        headers=_basic("admin", "secret-key"),
        data={"task": "now check the pricing page"},
    )
    assert resp.status_code == 200
    updated = await crud.get_session(session["id"])
    assert updated["task"] == "now check the pricing page"
    assert updated["status"] == "created"
    messages, _ = await crud.list_messages(session["id"], limit=10)
    assert any(
        m["type"] == "user_message" and m["summary"] == "now check the pricing page"
        for m in messages
    )
    submit.assert_called_once()


async def test_followup_rejected_for_non_keepalive_or_busy(client, monkeypatch):
    from unittest.mock import AsyncMock

    from app.db import crud

    monkeypatch.setattr("app.dashboard.routes.pool.submit", AsyncMock())
    plain = await crud.create_session(task="t", keep_alive=False)
    await crud.update_session(plain["id"], status="idle")
    resp = await client.post(
        f"/session/{plain['id']}/message",
        headers=_basic("admin", "secret-key"),
        data={"task": "x"},
    )
    assert resp.status_code == 400

    busy = await crud.create_session(task="t", keep_alive=True)
    await crud.update_session(busy["id"], status="running")
    resp = await client.post(
        f"/session/{busy['id']}/message",
        headers=_basic("admin", "secret-key"),
        data={"task": "x"},
    )
    assert resp.status_code == 409


async def test_session_detail_wires_the_streaming_component(client):
    from app.db import crud

    session = await crud.create_session(task="check the pricing page")
    resp = await client.get(
        f"/session/{session['id']}", headers=_basic("admin", "secret-key")
    )
    assert resp.status_code == 200
    assert 'id="stream-bar"' in resp.text
    assert "OpenBrowseAgents.StreamingResponse" in resp.text
    assert '<link rel="stylesheet" href="/static/openbrowse.css" />' in resp.text
    assert '<script defer src="/static/agents.js"></script>' in resp.text


def test_mdlite_bold_and_code():
    from app.dashboard.routes import _mdlite

    assert str(_mdlite("**bold** and `code`")) == "<strong>bold</strong> and <code>code</code>"


def test_mdlite_bold_spans_a_line_break():
    from app.dashboard.routes import _mdlite

    assert str(_mdlite("**foo\nbar** baz")) == "<strong>foo<br>bar</strong> baz"


def test_mdlite_unterminated_bold_stays_literal():
    from app.dashboard.routes import _mdlite

    out = str(_mdlite("checking the **access token with no closer"))
    assert out == "checking the **access token with no closer"
    assert "<strong>" not in out


def test_mdlite_unterminated_backtick_stays_literal():
    from app.dashboard.routes import _mdlite

    out = str(_mdlite("run `find_elements with no closer"))
    assert out == "run `find_elements with no closer"
    assert "<code>" not in out


def test_mdlite_closed_fence_renders_bounded_code_block():
    from app.dashboard.routes import _mdlite

    out = str(_mdlite("before\n```python\nprint(1)\n```\nafter"))
    assert out == "before<br><pre><code>print(1)\n</code></pre>after"


def test_mdlite_unclosed_fence_runs_to_end_as_code():
    from app.dashboard.routes import _mdlite

    out = str(_mdlite("before\n```python\nprint(1)\nstill going"))
    assert out == "before<br><pre><code>print(1)\nstill going</code></pre>"


def test_mdlite_bullet_list():
    from app.dashboard.routes import _mdlite

    out = str(_mdlite("- one\n- **two**\nplain after"))
    assert out == "<ul><li>one</li><li><strong>two</strong></li></ul><br>plain after"


def test_mdlite_escapes_html():
    from app.dashboard.routes import _mdlite

    assert str(_mdlite("<script>alert(1)</script>")) == "&lt;script&gt;alert(1)&lt;/script&gt;"


def test_activity_payload_carries_a_growing_stream_never_a_slice():
    from app.agent.activity import clear_activity, get_activity, set_activity

    sid = "activity-shape-test"
    clear_activity(sid)
    set_activity(sid, "💭 Thinking", spin=True, stream="checking the")
    first = get_activity(sid)
    assert first["stream"] == "checking the"
    assert first["spin"] is True
    assert first["label"] == "💭 Thinking"

    set_activity(sid, "💭 Thinking", spin=True, stream="checking the accessibility tree")
    second = get_activity(sid)
    assert second["stream"] == "checking the accessibility tree"
    assert second["stream"].startswith(first["stream"])

    set_activity(sid, "Running actions")
    third = get_activity(sid)
    assert third["stream"] is None
    assert third["label"] == "Running actions"
    clear_activity(sid)


class _StubRequest:
    """Enough Request for the SSE generator: resume header plus a liveness check."""

    def __init__(self, last_event_id=None):
        self.headers = {"last-event-id": last_event_id} if last_event_id else {}

    async def is_disconnected(self):
        return False


async def _drain_feed(session_id, *, last_event_id=None, want=1, timeout=3.0):
    """Pull frames off the feed generator directly — the ASGI transport never
    reports a disconnect, so an end-to-end stream would never terminate.
    """
    from app.dashboard.routes import sse_session_messages

    resp = await sse_session_messages(_StubRequest(last_event_id), session_id)
    gen = resp.body_iterator
    frames = []
    try:
        async with asyncio.timeout(timeout):
            async for ev in gen:
                frames.append(ev)
                if sum(1 for f in frames if f.get("event") == "messages") >= want:
                    break
    except (TimeoutError, asyncio.TimeoutError):
        pass
    finally:
        await gen.aclose()
    return frames


async def _seed_session_with_messages(n=3):
    from app.db import crud

    session = await crud.create_session(task="t", model="claude-sonnet-5")
    ids = []
    for i in range(n):
        m = await crud.create_message(
            session_id=session["id"], msg_type="event", summary=f"step {i}"
        )
        ids.append(m["id"])
    return session["id"], ids


def _message_frames(frames):
    return [f for f in frames if f.get("event") == "messages"]


async def test_sse_feed_replays_history_to_a_fresh_listener():
    sid, ids = await _seed_session_with_messages()

    rows = _message_frames(await _drain_feed(sid))
    assert rows, "a fresh listener must receive the backlog"
    for mid in ids:
        assert mid in rows[0]["data"]
    assert rows[0]["id"] == ids[-1], "the batch must carry a resumable id"


async def test_sse_feed_resumes_after_last_event_id_instead_of_replaying():
    sid, ids = await _seed_session_with_messages()

    rows = _message_frames(await _drain_feed(sid, last_event_id=ids[-1], timeout=2.0))
    assert rows == [], f"a reconnect must not replay delivered rows, got {rows}"


async def test_sse_feed_resumes_and_delivers_only_what_is_new():
    from app.db import crud

    sid, ids = await _seed_session_with_messages()
    fresh = await crud.create_message(session_id=sid, msg_type="event", summary="brand new")

    rows = _message_frames(await _drain_feed(sid, last_event_id=ids[-1]))
    assert len(rows) == 1
    assert fresh["id"] in rows[0]["data"]
    for mid in ids:
        assert mid not in rows[0]["data"]


def test_message_rows_carry_an_id_the_client_can_dedupe_on():
    from app.dashboard.routes import templates

    html = templates.get_template("_message_rows.html").render(
        messages=[{
            "id": "msg-abc-123",
            "type": "event",
            "data": "",
            "summary": "did a thing",
            "created_at": "2026-08-17T12:00:00+00:00",
        }],
        format_relative=lambda *_: "now",
    )
    assert 'data-mid="msg-abc-123"' in html


def test_streaming_response_caret_and_handoff_behaviour():
    import shutil
    import subprocess
    from pathlib import Path

    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed; the streaming-response harness needs it")
    harness = Path(__file__).parent / "fixtures" / "streaming_response_harness.mjs"
    proc = subprocess.run([node, str(harness)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stdout + proc.stderr
async def test_run_page_composer_keeps_native_submission(client):
    resp = await client.get("/", headers=_basic("admin", "secret-key"))
    assert resp.status_code == 200
    assert 'class="ob-composer-field"' in resp.text
    assert 'class="ob-composer-mirror"' in resp.text
    assert 'class="ob-composer-input"' in resp.text
    assert "native: true" in resp.text
    assert '<button class="run-btn" type="submit" disabled>Run</button>' in resp.text


async def test_followup_composer_replaces_the_hidden_input(client):
    from app.db import crud

    session = await crud.create_session(task="watch a page", keep_alive=True)
    resp = await client.get(
        f"/session/{session['id']}", headers=_basic("admin", "secret-key")
    )
    assert resp.status_code == 200
    assert 'class="ob-composer ob-composer-dock"' in resp.text
    assert 'id="followup-input"' in resp.text
    assert "OpenBrowseAgents.PromptInput" in resp.text
    assert 'style="display: none"' not in resp.text.split('id="followup-form"')[1][:200]


async def test_codeview_uses_the_shared_renderer(client):
    resp = await client.get("/codeview")
    assert resp.status_code == 200
    assert '<link rel="stylesheet" href="/static/openbrowse.css" />' in resp.text
    assert '<script defer src="/static/agents.js"></script>' in resp.text
    assert "agents.renderCode(code, 'python')" in resp.text
    assert "outerHTML" not in resp.text


def test_message_display_passes_sources_through():
    import json as _json

    from app.dashboard.routes import message_display

    row = {
        "type": "result",
        "summary": "read_pages: 2/2 pages -> pages.json",
        "data": _json.dumps({
            "category": "read",
            "action": "read_pages",
            "sources": [
                {"url": "https://www.bbc.co.uk/news/one", "title": "One"},
                {"url": "https://example.com/two", "title": ""},
            ],
        }),
    }
    md = message_display(row)
    assert len(md["sources"]) == 2
    assert md["sources"][0]["title"] == "One"


def test_domain_of_strips_the_www():
    from app.dashboard.routes import _domain_of

    assert _domain_of("https://www.bbc.co.uk/news/one") == "bbc.co.uk"
    assert _domain_of("https://example.com/two") == "example.com"
    assert _domain_of("") == ""


def test_sources_render_as_citations():
    import json

    from app.dashboard.routes import _format_relative_time, templates

    html = templates.get_template("_message_rows.html").render(
        messages=[{
            "type": "result",
            "created_at": "2026-08-17T15:00:00+00:00",
            "summary": "read_pages: 1/1 pages",
            "data": json.dumps({
                "category": "read",
                "action": "read_pages",
                "sources": [{"url": "https://www.bbc.co.uk/news/one", "title": "One"}],
            }),
        }],
        format_relative=_format_relative_time,
    )
    assert 'class="ob-cites"' in html
    assert "https://www.bbc.co.uk/news/one" in html
    assert ">bbc.co.uk<" in html
    assert "has-cards" in html
