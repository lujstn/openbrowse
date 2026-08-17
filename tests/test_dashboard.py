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
    resp = await client.post(
        "/settings",
        headers=_basic("admin", "secret-key"),
        data={
            "key": ["API_KEY", "MAX_CONCURRENT_SESSIONS", "OLD_VAR", "NEW_VAR"],
            "value": ["supersecret", "1", "", "hello"],
        },
    )
    assert resp.status_code == 303
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
