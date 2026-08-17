"""Tests for dashboard Basic auth, run form, and API fail-closed behaviour."""

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
        cloud_max_cost_factor=1.0,
    )
    monkeypatch.setattr("app.config.settings", test_settings)
    monkeypatch.setattr("app.db.models.settings", test_settings)
    monkeypatch.setattr("app.auth.settings", test_settings)
    monkeypatch.setattr("app.api.sessions.settings", test_settings)
    monkeypatch.setattr("app.dashboard.routes.settings", test_settings)
    monkeypatch.setattr("app.profiles.storage.settings", test_settings)
    (tmp_path / "data" / "profiles").mkdir(parents=True)
    await init_db()
    return test_settings


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


async def test_stop_endpoint_records_stop_event_in_feed(client):
    from app.db import crud

    session = await crud.create_session(task="scrape listings")
    sid = session["id"]
    await crud.update_session(sid, status="running")

    resp = await client.post(
        f"/session/{sid}/stop", headers=_basic("admin", "secret-key")
    )
    assert resp.status_code == 200

    messages, _ = await crud.list_messages(sid, limit=50)
    assert any(
        m["type"] == "event" and m["summary"] == "Stop requested from the dashboard"
        for m in messages
    )


async def test_session_detail_renders_failed_pill_not_warning(client):
    from app.db import crud

    session = await crud.create_session(task="scrape listings")
    sid = session["id"]
    await crud.update_session(sid, status="stopped", is_task_successful=0)

    resp = await client.get(f"/session/{sid}", headers=_basic("admin", "secret-key"))
    assert resp.status_code == 200
    body = resp.text
    assert "status-d-failed" in body
    assert "status-d-warning" not in body
    assert 'data-success="0"' in body


async def test_sessions_list_renders_failed_pill_not_warning(client):
    from app.db import crud

    session = await crud.create_session(task="scrape listings")
    sid = session["id"]
    await crud.update_session(sid, status="stopped", is_task_successful=0)

    resp = await client.get("/sessions", headers=_basic("admin", "secret-key"))
    assert resp.status_code == 200
    body = resp.text
    assert "status-d-failed" in body
    assert "status-d-warning" not in body


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


@patch("app.dashboard.routes.pool.submit", new_callable=AsyncMock)
async def test_dashboard_run_budget_not_scaled(mock_submit, client, setup, monkeypatch):
    import asyncio

    from app.dashboard import routes
    from app.db import crud

    monkeypatch.setattr(
        "app.api.sessions.settings", replace(setup, cloud_max_cost_factor=0.5)
    )
    resp = await client.post(
        "/run",
        data={
            "task": "Go to example.com",
            "model": "claude-sonnet-5",
            "max_cost_usd": "3",
        },
        headers=_basic("admin", "secret-key"),
    )
    assert resp.status_code == 303
    sid = resp.headers["location"].rsplit("/", 1)[-1]
    stored = await crud.get_session(sid)
    assert stored["max_cost_usd"] == 3.0
    if routes._dispatched_tasks:
        await asyncio.gather(*routes._dispatched_tasks, return_exceptions=True)


async def test_settings_page_offers_every_captcha_setting(client):
    """A setting only reachable by hand-editing .env is one most users never find."""
    resp = await client.get("/settings", headers=_basic("admin", "secret-key"))

    assert resp.status_code == 200
    assert "CAPTCHA solving" in resp.text
    for name in ("CAPSOLVER_API_KEY", "CAPTCHA_MAX_COST_USD"):
        assert name in resp.text, f"{name} is not offered on the settings page"

@pytest.fixture(autouse=True)
def _clear_live_sessions():
    from app.agent import live

    live._live.clear()
    yield
    live._live.clear()


async def test_run_records_what_the_user_asked(client, monkeypatch):
    from unittest.mock import AsyncMock

    from app.db import crud

    monkeypatch.setattr("app.dashboard.routes.pool.submit", AsyncMock())
    resp = await client.post(
        "/run",
        headers=_basic("admin", "secret-key"),
        data={"task": "Summarise the news", "model": "claude-sonnet-5"},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    sessions, _ = await crud.list_sessions(page=1, page_size=1)
    messages, _ = await crud.list_messages(sessions[0]["id"], limit=10)
    assert [m["summary"] for m in messages if m["type"] == "user_message"] == [
        "Summarise the news"
    ]


async def test_followup_continues_a_parked_session_without_a_new_run(client, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.agent import live
    from app.db import crud

    submit = AsyncMock()
    monkeypatch.setattr("app.dashboard.routes.pool.submit", submit)
    session = await crud.create_session(task="first task", keep_alive=True)
    await crud.update_session(session["id"], status="idle")
    entry = live.register(session["id"], SimpleNamespace())
    live.park(entry)

    resp = await client.post(
        f"/session/{session['id']}/message",
        headers=_basic("admin", "secret-key"),
        data={"task": "Is he really PM?"},
    )

    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "continued": True}
    assert entry.inbox.get_nowait() == "Is he really PM?"
    submit.assert_not_called()
    updated = await crud.get_session(session["id"])
    assert updated["status"] == "running"
    assert updated["task"] == "Is he really PM?"


async def test_followup_starts_a_fresh_run_once_the_browser_is_gone(client, monkeypatch):
    from unittest.mock import AsyncMock

    from app.db import crud

    submit = AsyncMock()
    monkeypatch.setattr("app.dashboard.routes.pool.submit", submit)
    session = await crud.create_session(task="first task", keep_alive=True)
    await crud.update_session(session["id"], status="stopped")

    resp = await client.post(
        f"/session/{session['id']}/message",
        headers=_basic("admin", "secret-key"),
        data={"task": "Is he really PM?"},
    )

    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "continued": False}
    submit.assert_called_once()
    assert (await crud.get_session(session["id"]))["status"] == "created"


async def test_followup_rejected_while_the_agent_is_mid_task(client, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.agent import live
    from app.db import crud

    submit = AsyncMock()
    monkeypatch.setattr("app.dashboard.routes.pool.submit", submit)
    session = await crud.create_session(task="first task", keep_alive=True)
    await crud.update_session(session["id"], status="idle")
    live.register(session["id"], SimpleNamespace())

    resp = await client.post(
        f"/session/{session['id']}/message",
        headers=_basic("admin", "secret-key"),
        data={"task": "Is he really PM?"},
    )

    assert resp.status_code == 409
    submit.assert_not_called()


async def test_stop_releases_a_parked_session(client, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.agent import live
    from app.db import crud

    cancel = AsyncMock()
    monkeypatch.setattr("app.dashboard.routes.pool.cancel", cancel)
    session = await crud.create_session(task="first task", keep_alive=True)
    await crud.update_session(session["id"], status="idle")
    stopped: list[bool] = []
    entry = live.register(session["id"], SimpleNamespace(stop=lambda: stopped.append(True)))
    live.park(entry)

    resp = await client.post(
        f"/session/{session['id']}/stop", headers=_basic("admin", "secret-key")
    )

    assert resp.status_code == 200
    assert entry.release.is_set()
    assert stopped == [True]
    cancel.assert_not_called()


async def test_session_page_offers_a_follow_up_after_the_browser_is_gone(client):
    from app.db import crud

    session = await crud.create_session(task="first task", keep_alive=True)
    await crud.update_session(session["id"], status="stopped")

    resp = await client.get(
        f"/session/{session['id']}", headers=_basic("admin", "secret-key")
    )

    assert resp.status_code == 200
    assert 'id="followup-form"' in resp.text
    assert "starts a fresh browser" in resp.text


async def test_followup_message_tops_the_session_budget_back_up(client, monkeypatch):
    """The follow-up box is where keep-alive is actually used, so a conversation
    started from the dashboard must not strangle itself as its pot drains."""
    from unittest.mock import AsyncMock

    from app.db import crud

    monkeypatch.setattr("app.dashboard.routes.pool.submit", AsyncMock())
    session = await crud.create_session(
        task="first task", keep_alive=True, max_cost_usd=1.5, default_max_cost_usd=1.5
    )
    await crud.update_session(session["id"], status="idle", total_cost_usd=1.2)

    resp = await client.post(
        f"/session/{session['id']}/message",
        headers=_basic("admin", "secret-key"),
        data={"task": "now check the pricing page"},
    )

    assert resp.status_code == 200
    assert (await crud.get_session(session["id"]))["max_cost_usd"] == 2.7


@patch("app.dashboard.routes.pool.submit", new_callable=AsyncMock)
async def test_a_dashboard_run_records_the_allowance_it_was_given(mock_submit, client):
    """Without the allowance on the row there is nothing to top a follow-up up by."""
    import asyncio

    from app.dashboard import routes
    from app.db import crud

    resp = await client.post(
        "/run",
        headers=_basic("admin", "secret-key"),
        data={"task": "go", "model": "claude-sonnet-5", "max_cost_usd": "2.25"},
    )

    assert resp.status_code == 303
    if routes._dispatched_tasks:
        await asyncio.gather(*routes._dispatched_tasks, return_exceptions=True)
    sessions, _ = await crud.list_sessions(page_size=1)
    assert sessions[0]["default_max_cost_usd"] == 2.25


async def test_session_detail_stacks_completions_rather_than_replacing_one(client):
    """Every turn of a keep-alive session finishes, so the page needs somewhere to
    keep the turns that already have, not a single slot the next one overwrites."""
    from app.db import crud

    session = await crud.create_session(task="scrape listings", keep_alive=True)

    body = (await client.get(f"/session/{session['id']}", headers=_basic("admin", "secret-key"))).text

    assert 'id="completion-cards"' in body
    assert "function archiveCompletion()" in body
    assert ".completion-card.collapsed .cc-out" in body
    assert "cc-caret" in body


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
