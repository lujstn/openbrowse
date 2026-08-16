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
        allow_insecure_no_auth=True,
    )
    monkeypatch.setattr("app.config.settings", test_settings)
    monkeypatch.setattr("app.db.models.settings", test_settings)
    monkeypatch.setattr("app.auth.settings", test_settings)
    monkeypatch.setattr("app.profiles.storage.settings", test_settings)
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


async def test_create_session_resolves_model_thinking_default(client):
    resp = await client.post("/v3/sessions", json={"model": "claude-sonnet-5"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["modelThinkingEffort"] == "high"
    assert data["thinkingEffort"] == "high"

    resp = await client.post("/v3/sessions", json={"model": "claude-opus-4-8"})
    assert resp.json()["modelThinkingEffort"] == "off"

    resp = await client.post("/v3/sessions", json={"model": "gpt-5.6-terra"})
    assert resp.json()["modelThinkingEffort"] == "default"


async def test_create_session_accepts_canonical_field_and_alias(client):
    resp = await client.post(
        "/v3/sessions",
        json={"model": "claude-sonnet-5", "modelThinkingEffort": "xhigh"},
    )
    assert resp.status_code == 200
    assert resp.json()["modelThinkingEffort"] == "xhigh"

    resp = await client.post(
        "/v3/sessions",
        json={"model": "claude-sonnet-5", "thinkingEffort": "off"},
    )
    assert resp.status_code == 200
    assert resp.json()["modelThinkingEffort"] == "off"


async def test_create_session_rejects_invalid_effort_per_model(client):
    resp = await client.post(
        "/v3/sessions",
        json={"model": "claude-fable-5", "modelThinkingEffort": "off"},
    )
    assert resp.status_code == 422
    assert "Valid values" in resp.json()["detail"]

    resp = await client.post(
        "/v3/sessions",
        json={"model": "gpt-5.6-terra", "modelThinkingEffort": "max"},
    )
    assert resp.status_code == 422

    resp = await client.post(
        "/v3/sessions",
        json={"model": "claude-sonnet-4-6", "modelThinkingEffort": "xhigh"},
    )
    assert resp.status_code == 422


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
