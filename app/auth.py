"""Bearer token authentication middleware."""

from __future__ import annotations

from fastapi import HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import settings

_bearer = HTTPBearer()


async def require_api_key(
    credentials: HTTPAuthorizationCredentials = Security(_bearer),
) -> str:
    """Validate the bearer token matches the configured API key."""
    if not settings.api_key:
        # No key configured — allow all (development mode)
        return "dev"
    if credentials.credentials != settings.api_key:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return credentials.credentials
