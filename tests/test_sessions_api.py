"""Tests for the v3-compatible Sessions API."""

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
        api_key="",
    )
    monkeypatch.setattr("app.config.settings", test_settings)
    monkeypatch.setattr("app.db.models.settings", test_settings)
    monkeypatch.setattr("app.auth.settings", test_settings)
    monkeypatch.setattr("app.api.profiles.settings", test_settings)
    (tmp_path / "data" / "profiles").mkdir(parents=True)
    await init_db()


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@patch("app.api.sessions.pool.submit", new_callable=AsyncMock)
async def test_create_session_with_task(mock_submit, client):
    resp = await client.post(
        "/v3/sessions",
        json={"task": "Go to google.com", "model": "claude-sonnet-4.6"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "created"
    assert data["model"] == "claude-sonnet-4.6"
    mock_submit.assert_called_once()


async def test_create_session_without_task(client):
    resp = await client.post("/v3/sessions", json={})
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "created"


async def test_list_sessions(client):
    await client.post("/v3/sessions", json={})
    await client.post("/v3/sessions", json={})
    resp = await client.get("/v3/sessions")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 2


async def test_get_session(client):
    create_resp = await client.post("/v3/sessions", json={})
    sid = create_resp.json()["id"]
    resp = await client.get(f"/v3/sessions/{sid}")
    assert resp.status_code == 200
    assert resp.json()["id"] == sid


async def test_get_nonexistent_session(client):
    resp = await client.get("/v3/sessions/nonexistent")
    assert resp.status_code == 404


async def test_list_messages_empty(client):
    create_resp = await client.post("/v3/sessions", json={})
    sid = create_resp.json()["id"]
    resp = await client.get(f"/v3/sessions/{sid}/messages")
    assert resp.status_code == 200
    assert resp.json()["messages"] == []
