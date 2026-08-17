"""Tests for per-IP failed-auth backoff and the health endpoint split."""

import base64
from dataclasses import replace

import pytest
from httpx import ASGITransport, AsyncClient

from app import auth_throttle
from app.auth_throttle import (
    BASE_LOCKOUT_SECONDS,
    FREE_ATTEMPTS,
    MAX_LOCKOUT_SECONDS,
    AuthThrottle,
)
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


def test_free_attempts_then_doubling_lockout():
    t = AuthThrottle()
    for _ in range(FREE_ATTEMPTS):
        t.record_failure("1.2.3.4")
    assert t.retry_after("1.2.3.4") == 0.0
    t.record_failure("1.2.3.4")
    first = t.retry_after("1.2.3.4")
    assert 0 < first <= BASE_LOCKOUT_SECONDS
    t.record_failure("1.2.3.4")
    assert t.retry_after("1.2.3.4") > first


def test_lockout_is_capped():
    t = AuthThrottle()
    for _ in range(FREE_ATTEMPTS + 40):
        t.record_failure("1.2.3.4")
    assert t.retry_after("1.2.3.4") <= MAX_LOCKOUT_SECONDS


def test_success_resets_the_record():
    t = AuthThrottle()
    for _ in range(FREE_ATTEMPTS + 3):
        t.record_failure("1.2.3.4")
    assert t.retry_after("1.2.3.4") > 0
    t.record_success("1.2.3.4")
    assert t.retry_after("1.2.3.4") == 0.0


def test_ips_are_tracked_independently():
    t = AuthThrottle()
    for _ in range(FREE_ATTEMPTS + 1):
        t.record_failure("1.2.3.4")
    assert t.retry_after("1.2.3.4") > 0
    assert t.retry_after("5.6.7.8") == 0.0


def test_table_is_bounded(monkeypatch):
    monkeypatch.setattr(auth_throttle, "MAX_TRACKED_IPS", 10)
    t = AuthThrottle()
    for i in range(30):
        t.record_failure(f"10.0.0.{i}")
    assert len(t._records) <= 10


async def test_api_locks_out_after_repeated_bad_keys(client):
    headers = {"X-Browser-Use-API-Key": "wrong", "X-Forwarded-For": "9.9.9.9"}
    for _ in range(FREE_ATTEMPTS):
        resp = await client.get("/v3/sessions", headers=headers)
        assert resp.status_code == 401
    resp = await client.get("/v3/sessions", headers=headers)
    assert resp.status_code == 401
    resp = await client.get("/v3/sessions", headers=headers)
    assert resp.status_code == 429
    assert "Retry-After" in resp.headers


async def test_lockout_blocks_even_the_right_key(client):
    for _ in range(FREE_ATTEMPTS + 1):
        await client.get(
            "/v3/sessions",
            headers={"X-Browser-Use-API-Key": "wrong", "X-Forwarded-For": "9.9.9.9"},
        )
    resp = await client.get(
        "/v3/sessions",
        headers={"X-Browser-Use-API-Key": "secret-key", "X-Forwarded-For": "9.9.9.9"},
    )
    assert resp.status_code == 429


async def test_other_ips_are_unaffected_by_a_lockout(client):
    for _ in range(FREE_ATTEMPTS + 1):
        await client.get(
            "/v3/sessions",
            headers={"X-Browser-Use-API-Key": "wrong", "X-Forwarded-For": "9.9.9.9"},
        )
    resp = await client.get(
        "/v3/sessions",
        headers={"X-Browser-Use-API-Key": "secret-key", "X-Forwarded-For": "8.8.8.8"},
    )
    assert resp.status_code == 200


async def test_requests_without_credentials_do_not_count(client):
    for _ in range(FREE_ATTEMPTS + 5):
        resp = await client.get("/v3/sessions", headers={"X-Forwarded-For": "9.9.9.9"})
        assert resp.status_code == 401
    resp = await client.get(
        "/v3/sessions",
        headers={"X-Browser-Use-API-Key": "secret-key", "X-Forwarded-For": "9.9.9.9"},
    )
    assert resp.status_code == 200


async def test_dashboard_locks_out_after_repeated_bad_passwords(client):
    headers = {**_basic("admin", "wrong"), "X-Forwarded-For": "9.9.9.9"}
    for _ in range(FREE_ATTEMPTS):
        resp = await client.get("/", headers=headers)
        assert resp.status_code == 401
    await client.get("/", headers=headers)
    await client.get("/", headers=headers)
    resp = await client.get("/", headers=headers)
    assert resp.status_code == 429


async def test_dashboard_success_resets_the_counter(client):
    bad = {**_basic("admin", "wrong"), "X-Forwarded-For": "9.9.9.9"}
    good = {**_basic("admin", "secret-key"), "X-Forwarded-For": "9.9.9.9"}
    for _ in range(FREE_ATTEMPTS):
        await client.get("/", headers=bad)
    assert (await client.get("/", headers=good)).status_code == 200
    for _ in range(FREE_ATTEMPTS):
        resp = await client.get("/", headers=bad)
        assert resp.status_code == 401


async def test_vnc_route_counts_failures_and_locks(client):
    headers = {**_basic("admin", "wrong"), "X-Forwarded-For": "9.9.9.9"}
    for _ in range(FREE_ATTEMPTS + 2):
        await client.get("/vnc/some-session/vnc.html", headers=headers)
    good = {**_basic("admin", "secret-key"), "X-Forwarded-For": "9.9.9.9"}
    resp = await client.get("/vnc/some-session/view", headers=good)
    assert resp.status_code == 401


async def test_health_is_bare_and_unauthenticated(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_health_details_requires_auth(client):
    resp = await client.get("/health/details")
    assert resp.status_code == 401
    resp = await client.get(
        "/health/details", headers={"X-Browser-Use-API-Key": "secret-key"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert "active_sessions" in body


def test_client_ip_prefers_forwarded_header():
    class Conn:
        def __init__(self, headers, host):
            self.headers = headers
            self.client = type("C", (), {"host": host})() if host else None

    assert (
        auth_throttle.client_ip(Conn({"x-forwarded-for": "2.2.2.2, 3.3.3.3"}, "127.0.0.1"))
        == "2.2.2.2"
    )
    assert auth_throttle.client_ip(Conn({}, "192.168.0.9")) == "192.168.0.9"
    assert auth_throttle.client_ip(Conn({}, None)) == "unknown"
