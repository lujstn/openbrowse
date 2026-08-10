"""v3-compatible Profiles API — mirrors cloud.browser-use.com."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth import require_api_key
from app.config import settings
from app.db import crud

router = APIRouter(prefix="/v3/profiles", tags=["profiles"])


# ── Pydantic models (match SDK types exactly) ────────────────────────


class ProfileCreateRequest(BaseModel):
    name: str | None = None
    userId: str | None = None


class ProfileUpdateRequest(BaseModel):
    name: str | None = None
    userId: str | None = None


class ProfileView(BaseModel):
    id: str
    userId: str | None = None
    name: str | None = None
    lastUsedAt: str | None = None
    createdAt: str
    updatedAt: str
    cookieDomains: list[str] | None = None


class ProfileListResponse(BaseModel):
    items: list[ProfileView]
    totalItems: int
    pageNumber: int
    pageSize: int


def _to_view(row: dict[str, Any]) -> ProfileView:
    """Convert a DB row to the SDK-compatible ProfileView."""
    cookie_domains: list[str] | None = None
    if row.get("storage_state_path"):
        state_file = settings.data_dir / row["storage_state_path"]
        if state_file.exists():
            try:
                state = json.loads(state_file.read_text())
                domains = {c.get("domain", "") for c in state.get("cookies", [])}
                cookie_domains = sorted(d for d in domains if d)
            except (json.JSONDecodeError, KeyError):
                pass

    return ProfileView(
        id=row["id"],
        userId=row.get("user_id"),
        name=row.get("name"),
        lastUsedAt=row.get("last_used_at"),
        createdAt=row["created_at"],
        updatedAt=row["updated_at"],
        cookieDomains=cookie_domains,
    )


# ── Endpoints ─────────────────────────────────────────────────────────


@router.post("", response_model=ProfileView, status_code=201)
async def create_profile(
    body: ProfileCreateRequest | None = None,
    _: str = Depends(require_api_key),
):
    body = body or ProfileCreateRequest()
    profile = await crud.create_profile(name=body.name, user_id=body.userId)
    # Create empty storage state file
    state_file = settings.data_dir / profile["storage_state_path"]
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps({"cookies": [], "origins": []}))
    return _to_view(profile)


@router.get("", response_model=ProfileListResponse)
async def list_profiles(
    page: int = 1,
    page_size: int = 20,
    query: str | None = None,
    _: str = Depends(require_api_key),
):
    profiles, total = await crud.list_profiles(page=page, page_size=page_size, query=query)
    return ProfileListResponse(
        items=[_to_view(p) for p in profiles],
        totalItems=total,
        pageNumber=page,
        pageSize=page_size,
    )


@router.get("/{profile_id}", response_model=ProfileView)
async def get_profile(profile_id: str, _: str = Depends(require_api_key)):
    profile = await crud.get_profile(profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")
    return _to_view(profile)


@router.patch("/{profile_id}", response_model=ProfileView)
@router.put("/{profile_id}", response_model=ProfileView)
async def update_profile(
    profile_id: str,
    body: ProfileUpdateRequest,
    _: str = Depends(require_api_key),
):
    existing = await crud.get_profile(profile_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Profile not found")
    fields: dict[str, Any] = {}
    if body.name is not None:
        fields["name"] = body.name
    if body.userId is not None:
        fields["user_id"] = body.userId
    updated = await crud.update_profile(profile_id, **fields)
    return _to_view(updated)


@router.delete("/{profile_id}", status_code=204)
async def delete_profile(profile_id: str, _: str = Depends(require_api_key)):
    existing = await crud.get_profile(profile_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Profile not found")
    # Delete storage state file
    if existing.get("storage_state_path"):
        state_file = settings.data_dir / existing["storage_state_path"]
        state_file.unlink(missing_ok=True)
    await crud.delete_profile(profile_id)
