"""Tests for dashboard Basic auth, run form, and API fail-closed behaviour."""

import asyncio
import base64
import json
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from openbrowse.config import settings
from openbrowse.db.models import init_db
from openbrowse.main import app


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
    monkeypatch.setattr("openbrowse.config.settings", test_settings)
    monkeypatch.setattr("openbrowse.db.models.settings", test_settings)
    monkeypatch.setattr("openbrowse.auth.settings", test_settings)
    monkeypatch.setattr("openbrowse.api.sessions.settings", test_settings)
    monkeypatch.setattr("openbrowse.dashboard.routes.settings", test_settings)
    monkeypatch.setattr("openbrowse.profiles.storage.settings", test_settings)
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


@patch("openbrowse.dashboard.routes.pool.submit_nowait")
async def test_run_creates_and_dispatches(mock_submit, client):
    import asyncio

    from openbrowse.dashboard import routes

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


@patch("openbrowse.dashboard.routes.pool.submit_nowait")
async def test_run_without_a_model_uses_the_configured_default(mock_submit, client):
    """The run form always posts a model, but the fallback behind it must be the
    configured default rather than a literal that drifts from it."""
    import asyncio

    from openbrowse.dashboard import routes
    from openbrowse.db import crud

    resp = await client.post(
        "/run", data={"task": "Go to example.com"}, headers=_basic("admin", "secret-key")
    )
    assert resp.status_code == 303
    session_id = resp.headers["location"].rsplit("/", 1)[-1]
    stored = await crud.get_session(session_id)
    assert stored["model"] == "gpt-5.6-terra"
    assert stored["reasoning_effort"] == "none"
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
    monkeypatch.setattr("openbrowse.auth.settings", closed)
    resp = await client.post("/v3/sessions", json={"task": "x"})
    assert resp.status_code == 401


async def test_profiles_page_shows_domains_not_user_id(client):
    from openbrowse.profiles.importer import import_profile

    await import_profile(
        "pid-1",
        {"cookies": [
            {"name": "s", "value": "v", "domain": ".workatastartup.com"},
            {"name": "t", "value": "v", "domain": ".ycombinator.com"},
        ], "origins": []},
        name="Acme",
    )
    resp = await client.get("/profiles", headers=_basic("admin", "secret-key"))
    assert resp.status_code == 200
    assert "Cookie Domains" in resp.text
    assert "<th>User ID</th>" not in resp.text
    assert 'name="user_id"' not in resp.text
    assert "2 domains" in resp.text


async def test_profile_create_makes_row_and_file(client, tmp_path):
    from openbrowse.db import crud

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
    from openbrowse.db import crud

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
    from openbrowse.db import crud

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
    from openbrowse.db import crud

    profile = await crud.create_profile(name="P")
    resp = await client.post(
        f"/profiles/{profile['id']}/edit",
        data={"name": "P", "user_id": "", "new_id": "../evil"},
        headers=_basic("admin", "secret-key"),
    )
    assert resp.status_code == 400
    assert await crud.get_profile(profile["id"]) is not None


async def test_profile_delete_cascade_nulls_session(client, tmp_path):
    from openbrowse.db import crud

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

    from openbrowse.db import crud

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
    from openbrowse.db import crud

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
    from openbrowse.db import crud

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
    from openbrowse.db import crud

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
    monkeypatch.setattr("openbrowse.dashboard.routes._ENV_PATH", env)
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
    monkeypatch.setattr("openbrowse.dashboard.routes._ENV_PATH", env)
    restarts = []
    monkeypatch.setattr(
        "openbrowse.dashboard.routes._schedule_restart", lambda: restarts.append(1)
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


async def test_settings_save_refused_while_sessions_running(client, tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("API_KEY=supersecret\n")
    monkeypatch.setattr("openbrowse.dashboard.routes._ENV_PATH", env)
    restarts = []
    monkeypatch.setattr(
        "openbrowse.dashboard.routes._schedule_restart", lambda: restarts.append(1)
    )
    monkeypatch.setattr(
        "openbrowse.dashboard.routes.pool", type("P", (), {"active_count": 1})()
    )
    resp = await client.post(
        "/settings",
        headers=_basic("admin", "secret-key"),
        data={"key": ["API_KEY", "NEW_VAR"], "value": ["supersecret", "hello"]},
    )
    assert resp.status_code == 200
    assert "Not saved" in resp.text
    assert "restart anyway" in resp.text
    assert "hello" in resp.text
    assert restarts == []
    assert "NEW_VAR" not in env.read_text()

    resp = await client.post(
        "/settings",
        headers=_basic("admin", "secret-key"),
        data={
            "key": ["API_KEY", "NEW_VAR"],
            "value": ["supersecret", "hello"],
            "force": "1",
        },
    )
    assert resp.status_code == 200
    assert restarts == [1]
    assert "NEW_VAR=hello" in env.read_text()


async def test_settings_save_atomic_leaves_no_tmp(client, tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("API_KEY=supersecret\n")
    monkeypatch.setattr("openbrowse.dashboard.routes._ENV_PATH", env)
    monkeypatch.setattr("openbrowse.dashboard.routes._schedule_restart", lambda: None)
    resp = await client.post(
        "/settings",
        headers=_basic("admin", "secret-key"),
        data={"key": ["API_KEY"], "value": ["supersecret"]},
    )
    assert resp.status_code == 200
    assert not (tmp_path / ".env.tmp").exists()
    assert env.read_text() == "API_KEY=supersecret\n"


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
    monkeypatch.setattr("openbrowse.dashboard.routes._ENV_PATH", env)
    now = _time.time()
    monkeypatch.setattr("openbrowse.dashboard.routes._STARTED_AT", now)

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

    from openbrowse.db import crud

    submit = MagicMock()
    monkeypatch.setattr("openbrowse.dashboard.routes.pool.submit_nowait", submit)
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

    from openbrowse.db import crud

    monkeypatch.setattr("openbrowse.dashboard.routes.pool.submit_nowait", MagicMock())
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


@patch("openbrowse.dashboard.routes.pool.submit_nowait")
async def test_dashboard_run_budget_not_scaled(mock_submit, client, setup, monkeypatch):
    import asyncio

    from openbrowse.dashboard import routes
    from openbrowse.db import crud

    monkeypatch.setattr(
        "openbrowse.api.sessions.settings", replace(setup, cloud_max_cost_factor=0.5)
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
    from openbrowse.agent import live

    live._live.clear()
    yield
    live._live.clear()


async def test_run_records_what_the_user_asked(client, monkeypatch):
    from unittest.mock import MagicMock

    from openbrowse.db import crud

    monkeypatch.setattr("openbrowse.dashboard.routes.pool.submit_nowait", MagicMock())
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
    from unittest.mock import MagicMock

    from openbrowse.agent import live
    from openbrowse.db import crud

    submit = MagicMock()
    monkeypatch.setattr("openbrowse.dashboard.routes.pool.submit_nowait", submit)
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
    from unittest.mock import MagicMock

    from openbrowse.db import crud

    submit = MagicMock()
    monkeypatch.setattr("openbrowse.dashboard.routes.pool.submit_nowait", submit)
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
    from unittest.mock import MagicMock

    from openbrowse.agent import live
    from openbrowse.db import crud

    submit = MagicMock()
    monkeypatch.setattr("openbrowse.dashboard.routes.pool.submit_nowait", submit)
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

    from openbrowse.agent import live
    from openbrowse.db import crud

    cancel = AsyncMock()
    monkeypatch.setattr("openbrowse.dashboard.routes.pool.cancel", cancel)
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
    from openbrowse.db import crud

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

    from openbrowse.db import crud

    monkeypatch.setattr("openbrowse.dashboard.routes.pool.submit_nowait", MagicMock())
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


@patch("openbrowse.dashboard.routes.pool.submit_nowait")
async def test_a_dashboard_run_records_the_allowance_it_was_given(mock_submit, client):
    """Without the allowance on the row there is nothing to top a follow-up up by."""
    import asyncio

    from openbrowse.dashboard import routes
    from openbrowse.db import crud

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
    from openbrowse.db import crud

    session = await crud.create_session(task="scrape listings", keep_alive=True)

    body = (await client.get(f"/session/{session['id']}", headers=_basic("admin", "secret-key"))).text

    assert 'id="completion-cards"' in body
    assert "function archiveCompletion()" in body
    assert ".completion-card.collapsed .cc-out" in body
    assert "cc-caret" in body


async def test_session_detail_wires_one_live_activity_surface(client):
    from openbrowse.db import crud

    session = await crud.create_session(task="check the pricing page")
    resp = await client.get(
        f"/session/{session['id']}", headers=_basic("admin", "secret-key")
    )
    assert resp.status_code == 200
    assert 'id="stream-bar"' in resp.text
    assert "OpenBrowseAgents.AgentActivity" in resp.text
    assert '<link rel="stylesheet" href="/static/openbrowse.css" />' in resp.text
    assert '<script defer src="/static/agents.js"></script>' in resp.text
    # the hand-rolled strip it replaced, so a second live surface cannot creep back
    for gone in ('id="activity-bar"', "act-spin", "act-label", "act-timer", "buildSpinBars"):
        assert gone not in resp.text, f"{gone} survived the swap"


def test_mdlite_bold_and_code():
    from openbrowse.dashboard.routes import _mdlite

    assert str(_mdlite("**bold** and `code`")) == "<strong>bold</strong> and <code>code</code>"


def test_mdlite_bold_spans_a_line_break():
    from openbrowse.dashboard.routes import _mdlite

    assert str(_mdlite("**foo\nbar** baz")) == "<strong>foo<br>bar</strong> baz"


def test_mdlite_unterminated_bold_stays_literal():
    from openbrowse.dashboard.routes import _mdlite

    out = str(_mdlite("checking the **access token with no closer"))
    assert out == "checking the **access token with no closer"
    assert "<strong>" not in out


def test_mdlite_unterminated_backtick_stays_literal():
    from openbrowse.dashboard.routes import _mdlite

    out = str(_mdlite("run `find_elements with no closer"))
    assert out == "run `find_elements with no closer"
    assert "<code>" not in out


def test_mdlite_closed_fence_renders_bounded_code_block():
    from openbrowse.dashboard.routes import _mdlite

    out = str(_mdlite("before\n```python\nprint(1)\n```\nafter"))
    assert out == "before<br><pre><code>print(1)\n</code></pre>after"


def test_mdlite_unclosed_fence_runs_to_end_as_code():
    from openbrowse.dashboard.routes import _mdlite

    out = str(_mdlite("before\n```python\nprint(1)\nstill going"))
    assert out == "before<br><pre><code>print(1)\nstill going</code></pre>"


def test_mdlite_bullet_list():
    from openbrowse.dashboard.routes import _mdlite

    out = str(_mdlite("- one\n- **two**\nplain after"))
    assert out == "<ul><li>one</li><li><strong>two</strong></li></ul><br>plain after"


def test_mdlite_escapes_html():
    from openbrowse.dashboard.routes import _mdlite

    assert str(_mdlite("<script>alert(1)</script>")) == "&lt;script&gt;alert(1)&lt;/script&gt;"


def test_activity_payload_carries_a_growing_stream_never_a_slice():
    from openbrowse.agent.activity import clear_activity, get_activity, set_activity

    sid = "activity-shape-test"
    clear_activity(sid)
    set_activity(sid, "Thinking", spin=True, stream="checking the")
    first = get_activity(sid)
    assert first["stream"] == "checking the"
    assert first["spin"] is True
    assert first["label"] == "Thinking"

    set_activity(sid, "Thinking", spin=True, stream="checking the accessibility tree")
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
    from openbrowse.dashboard.routes import sse_session_messages

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
    from openbrowse.db import crud

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
    from openbrowse.db import crud

    sid, ids = await _seed_session_with_messages()
    fresh = await crud.create_message(session_id=sid, msg_type="event", summary="brand new")

    rows = _message_frames(await _drain_feed(sid, last_event_id=ids[-1]))
    assert len(rows) == 1
    assert fresh["id"] in rows[0]["data"]
    for mid in ids:
        assert mid not in rows[0]["data"]


def test_message_rows_carry_an_id_the_client_can_dedupe_on():
    from openbrowse.dashboard.routes import templates

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


def test_agent_activity_states_and_handoff_behaviour():
    import shutil
    import subprocess
    from pathlib import Path

    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed; the agent-activity harness needs it")
    harness = Path(__file__).parent / "fixtures" / "agent_activity_harness.mjs"
    proc = subprocess.run([node, str(harness)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_activity_clock_survives_a_streaming_phase_and_resets_on_a_new_one():
    """A streaming phase re-pushes several times a second; if the clock restarted
    on each push no phase could ever report how long it took.
    """
    from openbrowse.agent.activity import clear_activity, get_activity, set_activity

    sid = "activity-clock-test"
    clear_activity(sid)

    set_activity(sid, "Thinking", spin=True, stream="a")
    started = get_activity(sid)["startedAt"]
    for text in ("a b", "a b c", "a b c d"):
        set_activity(sid, "Thinking", spin=True, stream=text)
        assert get_activity(sid)["startedAt"] == started, "the clock restarted mid-phase"

    set_activity(sid, "Thinking", stream="a b c d")
    assert get_activity(sid)["startedAt"] == started, "settling is the same phase"

    set_activity(sid, "Running actions")
    assert get_activity(sid)["startedAt"] != started, "a new phase starts a new clock"
    clear_activity(sid)


async def test_a_reasoning_row_lands_collapsed_like_any_other_step(client):
    """The live card already showed the thought; forcing the settled row open
    makes reasoning the one step type that shouts.
    """
    from openbrowse.db import crud

    session = await crud.create_session(task="check the pricing page")
    resp = await client.get(
        f"/session/{session['id']}", headers=_basic("admin", "secret-key")
    )
    assert resp.status_code == 200
    body = resp.text

    assert "OpenBrowseAgents.handoff.fadeRowIn(node)" in body
    assert "revealCards" not in body, "the settled reasoning row must not auto-expand"
    assert "ob-handoff-grow" not in body


def test_the_reasoning_row_headline_is_a_duration_not_a_half_sentence():
    from openbrowse.dashboard.routes import message_display

    display = message_display({
        "type": "event",
        "summary": "Reasoned for 21.6s",
        "data": json.dumps({
            "category": "reasoning",
            "action": "model_reasoning",
            "reasoning": "Everything looks complete - the headlines are in place. I'll finalise now.",
            "duration_s": 21.6,
        }),
    })

    assert display["summary"] == "Reasoned for 21.6s"
    assert "…" not in display["summary"]
    # the whole thought still rides along for the expanded card
    assert display["reasoning"].startswith("Everything looks complete")


def _render_row(category, action, summary):
    from openbrowse.dashboard.routes import message_display, templates

    return templates.get_template("_message_rows.html").render(
        messages=[{
            "id": "m1",
            "type": "event",
            "created_at": "2026-08-18T22:22:00+00:00",
            "summary": summary,
            "data": json.dumps({
                "category": category,
                "action": action,
                "duration_s": 21.6,
                "reasoning": "a whole thought",
            }),
        }],
        format_relative=lambda *a: "now",
        message_display=message_display,
    )


def test_a_reasoning_row_states_its_duration_once():
    """The headline already reads "Reasoned for 21.6s"; the badge beside it
    would say the same number a second time.
    """
    reasoning = _render_row("reasoning", "model_reasoning", "Reasoned for 21.6s")
    assert "Reasoned for 21.6s" in reasoning
    assert "msg-dur" not in reasoning, "the duration is printed twice on one row"
    assert "expanded" not in reasoning, "the row must render collapsed"

    # every other step keeps the badge, since its headline says nothing about time
    other = _render_row("read", "read_pages", "3 pages")
    assert "msg-dur" in other


def test_the_cost_breakdown_only_offers_itself_when_capsolver_charged():
    """A popover that always says CapSolver $0.0000 is a popover that never had
    anything to add.
    """
    from openbrowse.dashboard.routes import _format_duration, _format_relative_time, model_provider, templates

    def render(capsolver):
        return templates.get_template("_session_rows.html").render(
            sessions=[{
                "id": "s1", "task": "t", "status": "idle", "model": "claude-sonnet-5",
                "created_at": "2026-08-19T22:00:00+00:00",
                "updated_at": "2026-08-19T22:05:00+00:00",
                "total_cost_usd": 0.12, "llm_cost_usd": 0.1187,
                "capsolver_cost_usd": capsolver, "step_count": 3,
                "total_input_tokens": 1, "total_output_tokens": 2,
                "is_task_successful": True, "live_url": None, "keep_alive": False,
            }],
            model_provider=model_provider,
            format_relative=_format_relative_time,
            format_duration=_format_duration,
        )

    assert "has-breakdown" not in render(0)
    assert "has-breakdown" not in render(None)
    assert "has-breakdown" in render(0.0025)


def test_money_reads_in_cents_but_the_breakdown_reads_as_billed():
    from openbrowse.dashboard.routes import _usd, _usd4

    assert _usd(0.1187) == "0.12"
    assert _usd4(0.1187) == "0.1187"
    assert _usd(0.0032) == "0.01", "a real charge must never round down to nothing"
    assert _usd4(0.0032) == "0.0032"
    assert _usd4(None) == "0.0000"


async def test_a_finished_run_hides_its_copy_button_until_it_is_opened(client):
    """The copy control belongs to the output, so a collapsed card should not
    offer to copy something it is not showing.
    """
    from openbrowse.db import crud

    session = await crud.create_session(task="check the pricing page")
    resp = await client.get(
        f"/session/{session['id']}", headers=_basic("admin", "secret-key")
    )
    body = resp.text

    # the disclosure is a button at the end of the head, styled like a feed row's
    assert '<button class="cc-caret" type="button" aria-label="Expand result">' in body
    assert '<span class="cc-caret">' not in body, "the caret is no longer a glyph in the title"

    # copy lives inside the output block, which is what a collapsed card hides
    assert '<div class="cc-out"><button class="cc-copy"' in body
    assert ".completion-card.collapsed .cc-out" in body
    assert "COPY_ICON" in body and "COPIED_ICON" in body


def _capacity_info(**over):
    from dataclasses import replace as _replace

    from openbrowse.hostinfo import HostInfo

    base = HostInfo(
        cores=4, mem_total_kb=16 * 1024 * 1024, mem_available_kb=13 * 1024 * 1024,
        load1_per_core=0.1, psi_available=False, is_raspberry_pi=True,
        systemd=True, cgroup_memory=True, root_on_sd=True,
        resource_limits_set=False,
    )
    return _replace(base, **over)


async def test_tuned_machine_says_why_its_tuning_button_vanished(client, monkeypatch):
    """The sudoers grant names host_tune.sh by full path, so upgrading the
    package silently revokes it. Reading that as "the upgrade broke tuning" is
    the natural conclusion unless the page says otherwise."""
    monkeypatch.setattr(
        "openbrowse.hostinfo.probe",
        lambda: _capacity_info(
            resource_limits_set=True, psi_available=True, root_on_sd=False
        ),
    )
    monkeypatch.setattr("openbrowse.dashboard.routes._host_tune_available", lambda: False)

    resp = await client.get("/settings", headers=_basic("admin", "secret-key"))

    assert "no longer re-run the tuner" in resp.text
    assert "openbrowse tune --share most" in resp.text


async def test_settings_capacity_card_renders(client, monkeypatch):
    monkeypatch.setattr("openbrowse.hostinfo.probe", _capacity_info)
    monkeypatch.setattr("openbrowse.dashboard.routes._host_tune_available", lambda: False)
    resp = await client.get("/settings", headers=_basic("admin", "secret-key"))
    assert resp.status_code == 200
    assert "Capacity" in resp.text
    assert 'max="4"' in resp.text
    assert "openbrowse tune --share most" in resp.text
    assert "Apply recommended tuning" not in resp.text


async def test_settings_capacity_button_when_sudo_granted(client, monkeypatch):
    monkeypatch.setattr("openbrowse.hostinfo.probe", _capacity_info)
    monkeypatch.setattr("openbrowse.dashboard.routes._host_tune_available", lambda: True)
    resp = await client.get("/settings", headers=_basic("admin", "secret-key"))
    assert "Apply recommended tuning" in resp.text


async def test_settings_light_browser_recommended_hint(client, monkeypatch):
    monkeypatch.setattr("openbrowse.hostinfo.probe", _capacity_info)
    monkeypatch.setattr("openbrowse.dashboard.routes._host_tune_available", lambda: False)
    resp = await client.get("/settings", headers=_basic("admin", "secret-key"))
    assert resp.status_code == 200
    assert "Lighter browser" in resp.text
    assert "recommended for this machine" in resp.text
    assert 'value="CHROME_LIGHT_FLAGS"' in resp.text


async def test_settings_light_browser_marked_enabled_when_on(client, monkeypatch):
    from openbrowse.dashboard import routes as routes_mod

    monkeypatch.setattr("openbrowse.hostinfo.probe", _capacity_info)
    monkeypatch.setattr("openbrowse.dashboard.routes._host_tune_available", lambda: False)
    monkeypatch.setattr(
        "openbrowse.dashboard.routes.settings",
        replace(routes_mod.settings, chrome_light_flags=True),
    )
    resp = await client.get("/settings", headers=_basic("admin", "secret-key"))
    assert resp.status_code == 200
    assert "Lighter browser" in resp.text
    assert "enabled" in resp.text
    assert "recommended for this machine" not in resp.text


async def test_settings_host_tune_runs_script_and_shows_output(client, monkeypatch):
    import subprocess as _subprocess

    monkeypatch.setattr("openbrowse.hostinfo.probe", _capacity_info)
    monkeypatch.setattr("openbrowse.dashboard.routes._host_tune_available", lambda: True)
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return _subprocess.CompletedProcess(cmd, 0, stdout="doing: wrote override\ndone\n", stderr="")

    monkeypatch.setattr("openbrowse.dashboard.routes.subprocess.run", fake_run)
    resp = await client.post(
        "/settings/host-tune",
        headers=_basic("admin", "secret-key"),
        data={"share": "shared"},
    )
    assert resp.status_code == 200
    assert "Host tuning applied" in resp.text
    assert "wrote override" in resp.text
    assert any("--share" in c and "shared" in c for c in calls)
    assert any("--service" in c and "openbrowse.service" in c for c in calls)


async def test_settings_host_tune_rejects_bad_share(client, monkeypatch):
    import subprocess as _subprocess

    monkeypatch.setattr("openbrowse.hostinfo.probe", _capacity_info)
    monkeypatch.setattr("openbrowse.dashboard.routes._host_tune_available", lambda: False)
    seen = []

    def fake_run(cmd, **kwargs):
        seen.append(cmd)
        return _subprocess.CompletedProcess(cmd, 0, stdout="done", stderr="")

    monkeypatch.setattr("openbrowse.dashboard.routes.subprocess.run", fake_run)
    resp = await client.post(
        "/settings/host-tune",
        headers=_basic("admin", "secret-key"),
        data={"share": "everything"},
    )
    assert resp.status_code == 200
    assert any("most" in c for c in seen)
