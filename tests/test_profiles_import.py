"""Tests for profile cookie import: normalise, upsert, importer, and the v3 endpoint."""

from dataclasses import replace

import pytest
from httpx import ASGITransport, AsyncClient
from fastapi import FastAPI

from app.api.profiles import router
from app.config import settings
from app.db import crud
from app.db.models import init_db
from app.profiles import storage
from app.profiles.importer import ProfileImportError, import_bundle, import_profile


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
def app():
    _app = FastAPI()
    _app.include_router(router)
    return _app


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def test_normalize_strips_response_only_fields():
    raw = {
        "cookies": [
            {
                "name": "a", "value": "b", "domain": ".x.com", "path": "/",
                "secure": True, "httpOnly": False, "sameSite": "None", "expires": 123,
                "size": 44, "session": False, "sourceScheme": "Secure",
                "sourcePort": 443, "priority": "Medium",
            }
        ],
        "origins": [],
    }
    cookie = storage.normalize_storage_state(raw)["cookies"][0]
    assert "size" not in cookie and "session" not in cookie
    assert cookie["sameSite"] == "None"
    assert cookie["sourceScheme"] == "Secure"
    assert cookie["priority"] == "Medium"


def test_normalize_coerces_and_drops_samesite():
    raw = {"cookies": [
        {"name": "a", "value": "1", "domain": "x.com", "sameSite": "lax"},
        {"name": "b", "value": "1", "domain": "x.com", "sameSite": "unspecified"},
        {"name": "c", "value": "1", "domain": "x.com", "sameSite": "None", "secure": False},
    ]}
    by_name = {c["name"]: c for c in storage.normalize_storage_state(raw)["cookies"]}
    assert by_name["a"]["sameSite"] == "Lax"
    assert "sameSite" not in by_name["b"]
    assert "sameSite" not in by_name["c"]


def test_normalize_skips_malformed_and_preserves_origins():
    raw = {
        "cookies": [
            {"name": "", "value": "x", "domain": "x.com"},
            {"name": "ok", "domain": "x.com"},
            {"name": "ok", "value": "y"},
            {"name": "good", "value": "y", "domain": "x.com"},
        ],
        "origins": [{"origin": "https://x.com", "localStorage": [{"name": "k", "value": "v"}]}],
    }
    out = storage.normalize_storage_state(raw)
    assert [c["name"] for c in out["cookies"]] == ["good"]
    assert out["origins"][0]["localStorage"][0]["name"] == "k"


def test_normalize_rejects_non_dict():
    with pytest.raises(ValueError):
        storage.normalize_storage_state([1, 2, 3])


def test_cookie_domains_dedupes_and_strips_dots():
    state = {"cookies": [
        {"name": "a", "value": "1", "domain": ".example.com"},
        {"name": "b", "value": "1", "domain": "www.example.com"},
        {"name": "c", "value": "1", "domain": "x.com"},
    ]}
    assert storage.cookie_domains(state) == ["example.com", "www.example.com", "x.com"]


async def test_upsert_creates_with_explicit_id():
    row = await crud.upsert_profile("cloud-id-123", name="Personal")
    assert row["id"] == "cloud-id-123"
    assert row["name"] == "Personal"
    assert row["storage_state_path"] == "profiles/cloud-id-123.json"


async def test_upsert_updates_name_only_and_preserves_on_none():
    await crud.upsert_profile("pid", name="First")
    await crud.upsert_profile("pid", name="Second")
    assert (await crud.get_profile("pid"))["name"] == "Second"
    await crud.upsert_profile("pid", name=None)
    assert (await crud.get_profile("pid"))["name"] == "Second"


async def test_upsert_rejects_bad_id():
    with pytest.raises(ValueError):
        await crud.upsert_profile("../evil")


async def test_import_profile_writes_file_and_upserts():
    state = {"cookies": [{"name": "s", "value": "v", "domain": ".example.org"}], "origins": []}
    res = await import_profile("0bee43b4", state, name="Personal")
    assert res["created"] is True
    assert res["cookie_count"] == 1
    assert res["domains"] == ["example.org"]
    assert storage.profile_state_path("0bee43b4").exists()


async def test_import_single_storage_state_needs_default_id():
    with pytest.raises(ProfileImportError):
        await import_bundle({"cookies": [], "origins": []})


async def test_import_bundle_list_and_profiles_wrapper():
    bundle = [
        {"id": "a", "name": "A", "cookies": [{"name": "x", "value": "1", "domain": "a.com"}], "origins": []},
        {"id": "b", "name": "B", "storageState": {"cookies": [], "origins": []}},
    ]
    res = await import_bundle(bundle)
    assert {r["id"] for r in res} == {"a", "b"}
    assert await crud.get_profile("a") is not None
    res2 = await import_bundle({"profiles": [{"id": "c", "cookies": [], "origins": []}]})
    assert res2[0]["id"] == "c"


async def test_import_writes_backup_on_overwrite():
    await import_profile("pid", {"cookies": [], "origins": []})
    await import_profile("pid", {"cookies": [{"name": "n", "value": "v", "domain": "x.com"}], "origins": []})
    assert (storage.profile_state_path("pid").parent / "pid.json.import-bak").exists()


async def test_put_storage_state_creates_and_lists_domains(client):
    body = {"cookies": [
        {"name": "s", "value": "v", "domain": ".workatastartup.com", "sameSite": "Lax", "secure": True},
        {"name": "t", "value": "v", "domain": ".ycombinator.com", "sameSite": "Lax", "secure": True},
    ], "origins": []}
    resp = await client.put("/v3/profiles/9ebd2c83/storage-state", json=body)
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == "9ebd2c83"
    assert set(data["cookieDomains"]) == {"workatastartup.com", "ycombinator.com"}
    assert await crud.get_profile("9ebd2c83") is not None


async def test_put_storage_state_rejects_bad_id(client):
    resp = await client.put("/v3/profiles/..evil/storage-state", json={"cookies": [], "origins": []})
    assert resp.status_code == 400
