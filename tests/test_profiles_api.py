"""Tests for the v3-compatible Profiles API."""

from dataclasses import replace

import pytest
from httpx import ASGITransport, AsyncClient
from fastapi import FastAPI

from app.api.profiles import router
from app.config import settings
from app.db.models import init_db


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
def app():
    _app = FastAPI()
    _app.include_router(router)
    return _app


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_create_profile(client):
    resp = await client.post("/v3/profiles", json={"name": "Test"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "Test"
    assert "id" in data


async def test_list_profiles(client):
    await client.post("/v3/profiles", json={"name": "A"})
    await client.post("/v3/profiles", json={"name": "B"})
    resp = await client.get("/v3/profiles")
    assert resp.status_code == 200
    data = resp.json()
    assert data["totalItems"] == 2


async def test_get_profile(client):
    create_resp = await client.post("/v3/profiles", json={"name": "Test"})
    pid = create_resp.json()["id"]
    resp = await client.get(f"/v3/profiles/{pid}")
    assert resp.status_code == 200
    assert resp.json()["name"] == "Test"


async def test_delete_profile(client):
    create_resp = await client.post("/v3/profiles", json={"name": "Test"})
    pid = create_resp.json()["id"]
    resp = await client.delete(f"/v3/profiles/{pid}")
    assert resp.status_code == 204
    resp = await client.get(f"/v3/profiles/{pid}")
    assert resp.status_code == 404
