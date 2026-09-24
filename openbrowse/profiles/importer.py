"""Import BU Cloud profile cookie jars into local profiles.

Accepts either a single Playwright storage_state (``{"cookies": [...], "origins": [...]}``,
which needs a target id) or a bundle — a list of ``{id, name?, cookies?, origins?}`` /
``{id, name?, storageState: {...}}`` entries, or ``{"profiles": [...]}`` wrapping that list.
Each imported id is upserted (created if missing) and its normalised jar written to disk.
"""

from __future__ import annotations

from typing import Any

from openbrowse.db import crud
from openbrowse.profiles import store
from openbrowse.profiles.storage import cookie_domains, normalize_storage_state, profile_state_path


class ProfileImportError(ValueError):
    """Raised when an import payload cannot be interpreted."""


async def import_profile(
    profile_id: str,
    state: dict[str, Any],
    *,
    name: str | None = None,
    backup: bool = True,
) -> dict[str, Any]:
    """Upsert a profile and write its normalised storage state. Returns a result summary."""
    if not profile_id:
        raise ProfileImportError("profile id is required")
    existing = await crud.get_profile(profile_id)
    normalised = normalize_storage_state(state)
    await crud.upsert_profile(profile_id, name=name)
    path = profile_state_path(profile_id)
    async with store.lock(path):
        if backup and path.exists():
            path.with_name(path.name + ".import-bak").write_bytes(path.read_bytes())
        applied = store.save(path, store.ProfileData(normalised))
    return {
        "id": profile_id,
        "name": name if name is not None else (existing or {}).get("name"),
        "created": existing is None,
        "cookie_count": len(applied.state["cookies"]),
        "origin_count": len(applied.state["origins"]),
        "domains": cookie_domains(applied.state),
    }


def _detect_items(data: Any, default_id: str | None) -> list[dict[str, Any]]:
    if isinstance(data, dict) and isinstance(data.get("profiles"), list):
        return data["profiles"]
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and ("cookies" in data or "origins" in data):
        if not default_id:
            raise ProfileImportError("a single storage-state file needs a target profile id")
        return [{"id": default_id, "storageState": data}]
    raise ProfileImportError("unrecognised import payload shape")


async def import_bundle(
    data: Any,
    *,
    default_id: str | None = None,
    default_name: str | None = None,
    backup: bool = True,
) -> list[dict[str, Any]]:
    """Import one or many profiles from a detected payload shape. Returns per-profile summaries."""
    items = _detect_items(data, default_id)
    results: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            raise ProfileImportError("each profile entry must be a JSON object")
        profile_id = item.get("id") or default_id
        if not profile_id:
            raise ProfileImportError("profile entry is missing 'id'")
        name = item.get("name", default_name)
        state = item.get("storageState")
        if state is None:
            state = {"cookies": item.get("cookies", []), "origins": item.get("origins", [])}
        results.append(await import_profile(profile_id, state, name=name, backup=backup))
    return results
