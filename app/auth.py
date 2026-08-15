"""Bearer token authentication middleware."""

from __future__ import annotations

import base64
import hmac

from fastapi import Header, HTTPException, Security
from fastapi.security import (
    HTTPAuthorizationCredentials,
    HTTPBasic,
    HTTPBasicCredentials,
    HTTPBearer,
)

from app.config import settings

_bearer = HTTPBearer(auto_error=False)
_basic = HTTPBasic(auto_error=True)


async def require_api_key(
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer),
    x_browser_use_api_key: str | None = Header(default=None),
) -> str:
    """Validate the API key from the X-Browser-Use-API-Key header (SDK) or a bearer token."""
    if not settings.api_key:
        if settings.allow_insecure_no_auth:
            return "dev"
        raise HTTPException(status_code=401, detail="Server authentication is not configured")
    presented = x_browser_use_api_key
    if presented is None and credentials is not None:
        presented = credentials.credentials
    if presented is None or not hmac.compare_digest(presented, settings.api_key):
        raise HTTPException(status_code=401, detail="Invalid API key")
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


def dashboard_auth_ok(authorization: str | None) -> bool:
    """Verify a raw Basic ``Authorization`` header.

    For routes that cannot use the ``Security(HTTPBasic)`` dependency cleanly, such as
    WebSocket handshakes and the noVNC asset passthrough.
    """
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
    return check_dashboard_credentials(username, password)


async def require_dashboard_auth(
    credentials: HTTPBasicCredentials = Security(_basic),
) -> str:
    """Gate the dashboard behind HTTP Basic auth (dashboard user + password, or the API key)."""
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
        raise HTTPException(
            status_code=401,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username
