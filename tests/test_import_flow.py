"""Tests for the BU Cloud import flow: cloud-export helpers, job store, and routes."""

import asyncio
from dataclasses import replace

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.auth import require_dashboard_auth
from app.config import settings
from app.dashboard import import_routes
from app.db import crud
from app.db.models import init_db
from app.profiles import cloud_export, import_jobs
from app.profiles.storage import profile_state_path


@pytest.fixture(autouse=True)
async def setup(tmp_path, monkeypatch):
    test_settings = replace(
        settings,
        db_path=tmp_path / "t.db",
        data_dir=tmp_path / "data",
        profiles_dir=tmp_path / "data" / "profiles",
        api_key="",
        allow_insecure_no_auth=True,
    )
    monkeypatch.setattr("app.config.settings", test_settings)
    monkeypatch.setattr("app.db.models.settings", test_settings)
    monkeypatch.setattr("app.profiles.storage.settings", test_settings)
    (tmp_path / "data" / "profiles").mkdir(parents=True)
    await init_db()
    import_jobs.jobs._jobs.clear()


def test_candidate_origins():
    origins = cloud_export._candidate_origins(["www.example.com", ".example.org"])
    assert "https://example.com" in origins
    assert "https://www.example.com" in origins
    assert "https://example.org" in origins


def test_map_cookie_keeps_partition_key_and_defaults():
    c = cloud_export._map_cookie(
        {"name": "a", "value": "b", "domain": ".x.com", "path": "/", "partitionKey": {"t": "u"}}
    )
    assert c["partitionKey"] == {"t": "u"}
    c2 = cloud_export._map_cookie({"name": "a", "value": "b", "domain": ".x.com", "path": "/"})
    assert "partitionKey" not in c2
    assert c2["sameSite"] == "Lax" and c2["expires"] == -1


class _Resp:
    def __init__(self, status, data):
        self.status_code = status
        self._data = data

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("err", request=None, response=None)


class _Client:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, headers=None):
        return self._resp


def _patch_httpx(monkeypatch, resp):
    monkeypatch.setattr(cloud_export.httpx, "AsyncClient", lambda *a, **k: _Client(resp))


async def test_list_profiles_list_shape(monkeypatch):
    _patch_httpx(monkeypatch, _Resp(200, [{"id": "a", "name": "A", "cookieDomains": ["x.com"]}]))
    out = await cloud_export.list_cloud_profiles("tok")
    assert out == [{"id": "a", "name": "A", "cookieDomains": ["x.com"]}]


async def test_list_profiles_items_wrapper(monkeypatch):
    _patch_httpx(monkeypatch, _Resp(200, {"items": [{"id": "b", "name": "B"}]}))
    out = await cloud_export.list_cloud_profiles("tok")
    assert out[0]["id"] == "b" and out[0]["cookieDomains"] == []


async def test_list_profiles_rejects_bad_token(monkeypatch):
    _patch_httpx(monkeypatch, _Resp(401, {}))
    with pytest.raises(PermissionError):
        await cloud_export.list_cloud_profiles("tok")


def test_job_summary_excludes_cookie_values():
    job = import_jobs.jobs.new()
    job.staged["p"] = {"cookies": [{"name": "s", "value": "SECRET"}], "origins": []}
    job.profiles = [
        {"id": "p", "name": "P", "status": "ready", "cookie_count": 1, "origin_count": 0, "domains": ["x.com"]}
    ]
    summary = job.summary()
    assert "SECRET" not in str(summary)
    assert summary["profiles"][0]["cookieCount"] == 1


@pytest.fixture
def app():
    _app = FastAPI()
    _app.include_router(import_routes.router)
    _app.dependency_overrides[require_dashboard_auth] = lambda: "test"
    return _app


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _wait(job_id, want, timeout=3.0):
    loop = asyncio.get_event_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        job = import_jobs.jobs.get(job_id)
        if job and job.status in want:
            return job
        await asyncio.sleep(0.02)
    return import_jobs.jobs.get(job_id)


async def test_full_import_flow(client, monkeypatch):
    async def fake_list(token):
        return [{"id": "0bee", "name": "Personal", "cookieDomains": ["example.org"]}]

    async def fake_export(token, pid, on_log=None, on_progress=None):
        if on_log:
            on_log("read 2 cookies")
        if on_progress:
            on_progress(0, 3)
            on_progress(3, 3)
        return {
            "cookies": [
                {"name": "s", "value": "v", "domain": ".example.org"},
                {"name": "t", "value": "v", "domain": ".example.com"},
            ],
            "origins": [{"origin": "https://example.org", "localStorage": [{"name": "k", "value": "v"}]}],
        }

    monkeypatch.setattr(import_routes, "list_cloud_profiles", fake_list)
    monkeypatch.setattr(import_routes, "export_cloud_profile", fake_export)

    resp = await client.post("/profiles/import/start", data={"token": "bu_x"})
    assert resp.status_code == 200
    jid = resp.json()["jobId"]

    job = await _wait(jid, {"ready", "error"})
    assert job.status == "ready"
    assert job.profiles[0]["cookie_count"] == 2
    assert job.profiles[0]["progress"] == {"done": 3, "total": 3}
    assert "0bee" in job.staged

    resp = await client.post(f"/profiles/import/{jid}/confirm", json={"ids": ["0bee"]})
    assert resp.status_code == 200

    job = await _wait(jid, {"done", "error"})
    assert job.status == "done"
    assert not job.staged
    assert await crud.get_profile("0bee") is not None
    assert profile_state_path("0bee").exists()


async def test_start_requires_token(client):
    resp = await client.post("/profiles/import/start", data={"token": "   "})
    assert resp.status_code == 400


async def test_confirm_unknown_job(client):
    resp = await client.post("/profiles/import/nope/confirm", json={})
    assert resp.status_code == 404
