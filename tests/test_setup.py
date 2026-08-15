"""Tests for the first-run /setup screen."""

from dataclasses import replace

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import settings
from app.db.models import init_db
from app.main import app


@pytest.fixture
async def client(tmp_path, monkeypatch):
    test_settings = replace(
        settings,
        db_path=tmp_path / "test.db",
        data_dir=tmp_path / "data",
        profiles_dir=tmp_path / "data" / "profiles",
        api_key="",
        dashboard_password="",
        allow_insecure_no_auth=False,
    )
    for target in (
        "app.config.settings",
        "app.db.models.settings",
        "app.auth.settings",
        "app.dashboard.setup_routes.settings",
    ):
        monkeypatch.setattr(target, test_settings)
    monkeypatch.setattr(
        "app.dashboard.setup_routes._env_path", tmp_path / ".env"
    )
    await init_db()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c, tmp_path


async def test_setup_form_served_when_unconfigured(client):
    c, _ = client
    resp = await c.get("/setup")
    assert resp.status_code == 200
    assert "Welcome to OpenBrowse" in resp.text
    assert 'name="api_key"' in resp.text


async def test_setup_writes_env_and_shows_key_once(client):
    c, tmp_path = client
    resp = await c.post(
        "/setup",
        data={
            "api_key": "generated-abc123",
            "anthropic_api_key": "sk-ant-test",
            "openai_api_key": "",
            "dashboard_password": "hunter2",
        },
    )
    assert resp.status_code == 200
    assert "generated-abc123" in resp.text
    env = (tmp_path / ".env").read_text()
    assert "API_KEY=generated-abc123" in env
    assert "ANTHROPIC_API_KEY=sk-ant-test" in env
    assert "DASHBOARD_PASSWORD=hunter2" in env
    assert "OPENAI_API_KEY" not in env


async def test_setup_refuses_to_clobber_existing_env(client):
    c, tmp_path = client
    (tmp_path / ".env").write_text("API_KEY=already-here\n")
    resp = await c.post("/setup", data={"api_key": "new-key"})
    assert resp.status_code == 409
    assert (tmp_path / ".env").read_text() == "API_KEY=already-here\n"


async def test_setup_hidden_once_configured(tmp_path, monkeypatch):
    test_settings = replace(
        settings,
        db_path=tmp_path / "test.db",
        data_dir=tmp_path / "data",
        profiles_dir=tmp_path / "data" / "profiles",
        api_key="configured",
    )
    for target in (
        "app.config.settings",
        "app.db.models.settings",
        "app.auth.settings",
        "app.dashboard.setup_routes.settings",
    ):
        monkeypatch.setattr(target, test_settings)
    await init_db()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.get("/setup", follow_redirects=False)
        assert resp.status_code == 303
        resp = await c.post("/setup", data={"api_key": "x"}, follow_redirects=False)
        assert resp.status_code == 303
