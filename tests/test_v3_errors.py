"""Contract tests for v3 API error envelopes."""

from dataclasses import replace

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request

from openbrowse import auth_throttle
from openbrowse.api.errors import ErrorEnvelope, error_envelope, error_responses
from openbrowse.auth_throttle import FREE_ATTEMPTS
from openbrowse.config import settings
from openbrowse.db.models import init_db
from openbrowse.main import _http_error_handler, app


@pytest.fixture(autouse=True)
async def setup(tmp_path, monkeypatch):
    test_settings = replace(
        settings,
        db_path=tmp_path / "test.db",
        data_dir=tmp_path / "data",
        profiles_dir=tmp_path / "data" / "profiles",
        api_key="secret-key",
        allow_insecure_no_auth=False,
    )
    monkeypatch.setattr("openbrowse.config.settings", test_settings)
    monkeypatch.setattr("openbrowse.db.models.settings", test_settings)
    monkeypatch.setattr("openbrowse.auth.settings", test_settings)
    monkeypatch.setattr("openbrowse.api.sessions.settings", test_settings)
    monkeypatch.setattr("openbrowse.profiles.storage.settings", test_settings)
    (tmp_path / "data" / "profiles").mkdir(parents=True)
    await init_db()
    auth_throttle.throttle.reset()


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _assert_envelope(response, code: str, detail):
    body = response.json()
    assert set(body) == {"code", "message", "resolution", "detail"}
    assert body["code"] == code
    assert body["detail"] == detail
    assert isinstance(body["message"], str)
    assert isinstance(body["resolution"], str)


@pytest.mark.parametrize(
    ("status_code", "detail", "code"),
    [
        (400, "Storage state must contain cookies", "INVALID_STORAGE_STATE"),
        (401, "Server authentication is not configured", "AUTH_NOT_CONFIGURED"),
        (401, "Invalid API key", "INVALID_API_KEY"),
        (
            429,
            "Too many failed authentication attempts. Try again later.",
            "AUTH_RATE_LIMITED",
        ),
        (404, "Session not found", "SESSION_NOT_FOUND"),
        (404, "Profile not found", "PROFILE_NOT_FOUND"),
        (404, "Not Found", "ENDPOINT_NOT_FOUND"),
        (422, "Session is stopped, not idle", "SESSION_NOT_IDLE"),
        (422, "Session is paused, not idle", "SESSION_NOT_IDLE"),
        (422, "Task is required when targeting an existing session", "TASK_REQUIRED"),
        (422, "Unsupported reasoning effort", "INVALID_REASONING_EFFORT"),
        (422, "Session is corrupted", "INVALID_REASONING_EFFORT"),
        (401, "Not authenticated", "UNEXPECTED_ERROR"),
        (409, "Conflicting request", "UNEXPECTED_ERROR"),
    ],
)
def test_v3_error_codes_are_stable(status_code, detail, code):
    envelope = error_envelope(status_code, detail)

    assert envelope.code == code
    assert envelope.detail == detail
    assert envelope.resolution


async def test_v3_validation_error_preserves_detail_array(client):
    response = await client.get(
        "/v3/sessions?page=0", headers={"X-Browser-Use-API-Key": "secret-key"}
    )

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "REQUEST_VALIDATION_FAILED"
    assert isinstance(body["detail"], list)


async def test_v3_auth_error_is_structured(client):
    response = await client.get("/v3/sessions")

    assert response.status_code == 401
    _assert_envelope(response, "INVALID_API_KEY", "Invalid API key")


async def test_v3_session_and_profile_errors_are_structured(client):
    headers = {"X-Browser-Use-API-Key": "secret-key"}

    session = await client.get("/v3/sessions/missing", headers=headers)
    profile = await client.get("/v3/profiles/missing", headers=headers)

    assert session.status_code == 404
    _assert_envelope(session, "SESSION_NOT_FOUND", "Session not found")
    assert profile.status_code == 404
    _assert_envelope(profile, "PROFILE_NOT_FOUND", "Profile not found")


async def test_v3_route_miss_is_structured(client):
    response = await client.get("/v3/missing")

    assert response.status_code == 404
    _assert_envelope(response, "ENDPOINT_NOT_FOUND", "Not Found")


async def test_v3_rate_limit_preserves_retry_after(client):
    headers = {"X-Browser-Use-API-Key": "wrong", "X-Forwarded-For": "9.9.9.9"}
    for _ in range(FREE_ATTEMPTS + 2):
        response = await client.get("/v3/sessions", headers=headers)

    assert response.status_code == 429
    assert response.headers["Retry-After"]
    _assert_envelope(
        response,
        "AUTH_RATE_LIMITED",
        "Too many failed authentication attempts. Try again later.",
    )


async def test_legacy_error_shape_is_unchanged(client):
    response = await client.get("/health/details")

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid API key"}


def test_v3_openapi_errors_and_operation_ids_are_documented():
    schema = app.openapi()
    create_session = schema["paths"]["/v3/sessions"]["post"]
    rate_limited = create_session["responses"]["429"]

    assert create_session["operationId"] == "createSession"
    assert rate_limited["headers"]["Retry-After"]["schema"]["type"] == "integer"
    assert schema["components"]["schemas"]["ErrorEnvelope"]


@pytest.mark.parametrize("detail", ["Session is corrupted", "Session is running"])
def test_session_idle_match_requires_the_not_idle_suffix(detail):
    envelope = error_envelope(422, detail)

    assert envelope.code == "INVALID_REASONING_EFFORT"


@pytest.mark.parametrize("status_code", [401, 403, 409, 500, 503])
def test_unmapped_status_never_raises(status_code):
    envelope = error_envelope(status_code, "an unanticipated detail")

    assert envelope.code == "UNEXPECTED_ERROR"
    assert envelope.resolution


def test_error_responses_tolerates_undocumented_status():
    responses = error_responses(405, 401)

    assert responses[405]["model"] is ErrorEnvelope
    assert responses[401]["description"] == "Authentication failed."


@pytest.mark.parametrize("status_code", [204, 304])
async def test_no_body_status_yields_empty_response(status_code):
    scope = {"type": "http", "method": "GET", "path": "/v3/x", "headers": []}
    response = await _http_error_handler(
        Request(scope), StarletteHTTPException(status_code=status_code)
    )

    assert response.status_code == status_code
    assert response.body == b""
