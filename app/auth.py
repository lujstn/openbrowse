"""Bearer token authentication middleware."""

from __future__ import annotations

import hmac

from fastapi import Header, HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import settings

_bearer = HTTPBearer(auto_error=False)


async def require_api_key(
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer),
    x_browser_use_api_key: str | None = Header(default=None),
) -> str:
    """Validate the API key from the X-Browser-Use-API-Key header (SDK) or a bearer token."""
    if not settings.api_key:
        return "dev"
    presented = x_browser_use_api_key
    if presented is None and credentials is not None:
        presented = credentials.credentials
    if presented is None or not hmac.compare_digest(presented, settings.api_key):
        raise HTTPException(status_code=401, detail="Invalid API key")
    return presented
