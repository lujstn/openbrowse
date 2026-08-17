"""Bearer token authentication middleware."""

from __future__ import annotations

import base64
import hmac

from fastapi import Header, HTTPException, Request, Security
from fastapi.security import (
    HTTPAuthorizationCredentials,
    HTTPBasic,
    HTTPBasicCredentials,
    HTTPBearer,
)
from starlette.requests import HTTPConnection

from app import auth_throttle
from app.config import settings

_bearer = HTTPBearer(auto_error=False)
_basic = HTTPBasic(auto_error=True)


async def require_api_key(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer),
    x_browser_use_api_key: str | None = Header(default=None),
) -> str:
    """Validate the API key from the X-Browser-Use-API-Key header (SDK) or a bearer token."""
    ip = auth_throttle.client_ip(request)
    auth_throttle.enforce(ip)
    if not settings.api_key:
        if settings.allow_insecure_no_auth:
            return "dev"
        raise HTTPException(status_code=401, detail="Server authentication is not configured")
    presented = x_browser_use_api_key
    if presented is None and credentials is not None:
        presented = credentials.credentials
    if presented is None:
        raise HTTPException(status_code=401, detail="Invalid API key")
    if not hmac.compare_digest(presented, settings.api_key):
        auth_throttle.throttle.record_failure(ip)
        raise HTTPException(status_code=401, detail="Invalid API key")
    auth_throttle.throttle.record_success(ip)
    return presented


def _expected_dashboard_password() -> str:
    return settings.dashboard_password or settings.api_key


def check_dashboard_credentials(username: str, password: str) -> bool:
    """Constant-time check of Basic credentials against the dashboard user and password."""
    expected = _expected_dashboard_password()
    if not expected:
        return settings.allow_insecure_no_auth
    user_ok = hmac.compare_digest(username, settings.dashboard_user)
    pass_ok = hmac.compare_digest(password, expected)
    return user_ok and pass_ok


def dashboard_auth_ok(authorization: str | None, conn: HTTPConnection | None = None) -> bool:
    """Verify a raw Basic ``Authorization`` header.

    For routes that cannot use the ``Security(HTTPBasic)`` dependency cleanly, such as
    WebSocket handshakes and the noVNC asset passthrough. When ``conn`` is given,
    failed attempts with presented credentials count towards the per-IP backoff
    and locked-out IPs are refused outright.
    """
    ip = auth_throttle.client_ip(conn) if conn is not None else None
    if ip is not None and auth_throttle.throttle.retry_after(ip) > 0:
        return False
    if not _expected_dashboard_password():
        return settings.allow_insecure_no_auth
    if not authorization:
        return False
    scheme, _, encoded = authorization.partition(" ")
    if scheme.lower() != "basic" or not encoded:
        return False
    try:
        username, sep, password = base64.b64decode(encoded).decode("utf-8").partition(":")
    except (ValueError, UnicodeDecodeError):
        return False
    if not sep:
        return False
    ok = check_dashboard_credentials(username, password)
    if ip is not None:
        if ok:
            auth_throttle.throttle.record_success(ip)
        else:
            auth_throttle.throttle.record_failure(ip)
    return ok


async def require_dashboard_auth(
    request: Request,
    credentials: HTTPBasicCredentials = Security(_basic),
) -> str:
    """Gate the dashboard behind HTTP Basic auth (dashboard user + password, or the API key)."""
    ip = auth_throttle.client_ip(request)
    auth_throttle.enforce(ip)
    if not _expected_dashboard_password():
        if settings.allow_insecure_no_auth:
            return "dev"
        raise HTTPException(
            status_code=503,
            detail=(
                "Dashboard authentication is not configured — open /setup in "
                "your browser to configure this instance."
            ),
        )
    if not check_dashboard_credentials(credentials.username, credentials.password):
        auth_throttle.throttle.record_failure(ip)
        raise HTTPException(
            status_code=401,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    auth_throttle.throttle.record_success(ip)
    return credentials.username
