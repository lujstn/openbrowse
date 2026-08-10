"""Bearer token authentication middleware."""

from __future__ import annotations

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


async def require_dashboard_auth(
    credentials: HTTPBasicCredentials = Security(_basic),
) -> str:
    """Gate the dashboard behind HTTP Basic auth (dashboard user + password, or the API key)."""
    password = settings.dashboard_password or settings.api_key
    if not password:
        if settings.allow_insecure_no_auth:
            return "dev"
        raise HTTPException(status_code=503, detail="Dashboard authentication is not configured")
    user_ok = hmac.compare_digest(credentials.username, settings.dashboard_user)
    pass_ok = hmac.compare_digest(credentials.password, password)
    if not (user_ok and pass_ok):
        raise HTTPException(
            status_code=401,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username
