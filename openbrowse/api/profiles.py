"""v3-compatible Profiles API — mirrors cloud.browser-use.com."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from openbrowse.auth import require_api_key
from openbrowse.api.errors import error_responses
from openbrowse.db import crud
from openbrowse.profiles import storage
from openbrowse.profiles.importer import ProfileImportError, import_profile

router = APIRouter(prefix="/v3/profiles", tags=["profiles"])


# ── Pydantic models (match SDK types exactly) ────────────────────────


class ProfileCreateRequest(BaseModel):
    name: str | None = None
    userId: str | None = None


class ProfileUpdateRequest(BaseModel):
    name: str | None = None
    userId: str | None = None


class StorageStateBody(BaseModel):
    cookies: list[dict[str, Any]] = []
    origins: list[dict[str, Any]] = []


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
    state = storage.read_state_file(row.get("storage_state_path"))
    cookie_domains = storage.cookie_domains(state) if state is not None else None

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


@router.post(
    "",
    response_model=ProfileView,
    status_code=201,
    responses=error_responses(401, 422, 429),
    operation_id="createProfile",
)
async def create_profile(
    body: ProfileCreateRequest | None = None,
    _: str = Depends(require_api_key),
):
    """Create an empty profile, with no cookies and no per-origin storage yet. Fill it by importing a cookie jar, or by letting a session log in while using it."""
    body = body or ProfileCreateRequest()
    profile = await crud.create_profile(name=body.name, user_id=body.userId)
    storage.write_profile_state(profile["id"], {"cookies": [], "origins": []}, backup=False)
    return _to_view(profile)


@router.get(
    "",
    response_model=ProfileListResponse,
    responses=error_responses(401, 422, 429),
    operation_id="listProfiles",
)
async def list_profiles(
    page: int = 1,
    page_size: int = 20,
    query: str | None = None,
    _: str = Depends(require_api_key),
):
    """List profiles, paginated, with an optional query to narrow the results."""
    profiles, total = await crud.list_profiles(page=page, page_size=page_size, query=query)
    return ProfileListResponse(
        items=[_to_view(p) for p in profiles],
        totalItems=total,
        pageNumber=page,
        pageSize=page_size,
    )


@router.get(
    "/{profile_id}",
    response_model=ProfileView,
    responses=error_responses(401, 404, 429),
    operation_id="getProfile",
)
async def get_profile(profile_id: str, _: str = Depends(require_api_key)):
    """Fetch a single profile by id. Returns 404 if no profile has that id."""
    profile = await crud.get_profile(profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")
    return _to_view(profile)


@router.patch(
    "/{profile_id}",
    response_model=ProfileView,
    responses=error_responses(401, 404, 422, 429),
    operation_id="patchProfile",
)
@router.put(
    "/{profile_id}",
    response_model=ProfileView,
    responses=error_responses(401, 404, 422, 429),
    operation_id="updateProfile",
)
async def update_profile(
    profile_id: str,
    body: ProfileUpdateRequest,
    _: str = Depends(require_api_key),
):
    """Update a profile's editable fields. PUT and PATCH behave identically here: both apply only the fields present in the request body."""
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


@router.delete(
    "/{profile_id}",
    status_code=204,
    responses=error_responses(401, 404, 429),
    operation_id="deleteProfile",
)
async def delete_profile(profile_id: str, _: str = Depends(require_api_key)):
    """Delete a profile and the browser state stored against it. The cookies and per-origin storage go with it."""
    existing = await crud.get_profile(profile_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Profile not found")
    storage.profile_state_path(profile_id).unlink(missing_ok=True)
    await crud.delete_profile(profile_id)


@router.put(
    "/{profile_id}/storage-state",
    response_model=ProfileView,
    responses=error_responses(400, 401, 422, 429),
    operation_id="putProfileStorageState",
)
async def put_storage_state(
    profile_id: str,
    body: StorageStateBody,
    _: str = Depends(require_api_key),
):
    """Import a cookie jar into a profile, creating the profile if it does not exist yet."""
    try:
        await import_profile(profile_id, body.model_dump(), backup=True)
    except (ProfileImportError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    profile = await crud.get_profile(profile_id)
    return _to_view(profile)
