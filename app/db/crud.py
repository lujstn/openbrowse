"""CRUD operations for sessions, messages, and profiles."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

import aiosqlite

from app.db.models import get_db

_SESSION_COLUMNS = {
    "status", "model", "task", "title", "output", "output_schema",
    "step_count", "last_step_summary", "is_task_successful", "live_url",
    "profile_id", "sensitive_data", "max_cost_usd", "total_input_tokens",
    "total_output_tokens", "llm_cost_usd", "total_cost_usd",
    "screenshot_path", "display_num", "system_prompt_extension", "updated_at",
}

_PROFILE_COLUMNS = {
    "name", "user_id", "storage_state_path", "last_used_at", "updated_at",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return str(uuid.uuid4())


# ── Sessions ──────────────────────────────────────────────────────────


async def create_session(
    *,
    task: str | None = None,
    model: str = "claude-sonnet-4-6",
    profile_id: str | None = None,
    output_schema: dict[str, Any] | None = None,
    sensitive_data: dict[str, str] | None = None,
    system_prompt_extension: str | None = None,
    max_cost_usd: float | None = None,
) -> dict[str, Any]:
    session_id = _new_id()
    now = _now()
    db = await get_db()
    try:
        await db.execute(
            """INSERT INTO sessions
               (id, status, model, task, profile_id, output_schema,
                sensitive_data, system_prompt_extension, max_cost_usd,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                session_id,
                "created",
                model,
                task,
                profile_id,
                json.dumps(output_schema) if output_schema else None,
                json.dumps(sensitive_data) if sensitive_data else None,
                system_prompt_extension,
                max_cost_usd,
                now,
                now,
            ),
        )
        await db.commit()
        return await get_session(session_id, db=db)
    finally:
        await db.close()


async def get_session(
    session_id: str, *, db: aiosqlite.Connection | None = None
) -> dict[str, Any] | None:
    close = db is None
    if db is None:
        db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM sessions WHERE id = ?", (session_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None
    finally:
        if close:
            await db.close()


async def list_sessions(
    *, page: int = 1, page_size: int = 20
) -> tuple[list[dict[str, Any]], int]:
    db = await get_db()
    try:
        cursor = await db.execute("SELECT COUNT(*) FROM sessions")
        total = (await cursor.fetchone())[0]
        offset = (page - 1) * page_size
        cursor = await db.execute(
            "SELECT * FROM sessions ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (page_size, offset),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows], total
    finally:
        await db.close()


async def update_session(session_id: str, **fields: Any) -> dict[str, Any] | None:
    if not fields:
        return await get_session(session_id)
    invalid = set(fields) - _SESSION_COLUMNS
    if invalid:
        raise ValueError(f"Invalid column names: {invalid}")
    fields["updated_at"] = _now()
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [session_id]
    db = await get_db()
    try:
        await db.execute(
            f"UPDATE sessions SET {set_clause} WHERE id = ?", values
        )
        await db.commit()
        return await get_session(session_id, db=db)
    finally:
        await db.close()


async def delete_session(session_id: str) -> bool:
    db = await get_db()
    try:
        cursor = await db.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        await db.commit()
        return cursor.rowcount > 0
    finally:
        await db.close()


# ── Messages ──────────────────────────────────────────────────────────


async def create_message(
    *,
    session_id: str,
    role: str = "ai",
    data: str = "",
    msg_type: str = "",
    summary: str = "",
    screenshot_path: str | None = None,
    hidden: bool = False,
) -> dict[str, Any]:
    msg_id = _new_id()
    now = _now()
    db = await get_db()
    try:
        await db.execute(
            """INSERT INTO messages
               (id, session_id, role, data, type, summary, screenshot_path, hidden, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (msg_id, session_id, role, data, msg_type, summary, screenshot_path, int(hidden), now),
        )
        # Bump session step_count and last_step_summary atomically with the insert
        await db.execute(
            """UPDATE sessions
               SET step_count = step_count + 1, last_step_summary = ?, updated_at = ?
               WHERE id = ?""",
            (summary, now, session_id),
        )
        await db.commit()
        return {
            "id": msg_id,
            "session_id": session_id,
            "role": role,
            "data": data,
            "type": msg_type,
            "summary": summary,
            "screenshot_path": screenshot_path,
            "hidden": hidden,
            "created_at": now,
        }
    finally:
        await db.close()


async def list_messages(
    session_id: str,
    *,
    after: str | None = None,
    before: str | None = None,
    limit: int = 100,
) -> tuple[list[dict[str, Any]], bool]:
    db = await get_db()
    try:
        conditions = ["session_id = ?"]
        params: list[Any] = [session_id]
        if after:
            conditions.append("created_at > (SELECT created_at FROM messages WHERE id = ?)")
            params.append(after)
        if before:
            conditions.append("created_at < (SELECT created_at FROM messages WHERE id = ?)")
            params.append(before)
        where = " AND ".join(conditions)
        query = f"SELECT * FROM messages WHERE {where} ORDER BY created_at ASC LIMIT ?"
        params.append(limit + 1)  # fetch one extra to check hasMore
        cursor = await db.execute(query, params)
        rows = await cursor.fetchall()
        items = [dict(r) for r in rows[:limit]]
        has_more = len(rows) > limit
        return items, has_more
    finally:
        await db.close()


# ── Profiles ──────────────────────────────────────────────────────────


async def create_profile(
    *, name: str | None = None, user_id: str | None = None
) -> dict[str, Any]:
    profile_id = _new_id()
    now = _now()
    storage_path = f"profiles/{profile_id}.json"
    db = await get_db()
    try:
        await db.execute(
            """INSERT INTO profiles (id, name, user_id, storage_state_path, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (profile_id, name, user_id, storage_path, now, now),
        )
        await db.commit()
        return await get_profile(profile_id, db=db)
    finally:
        await db.close()


async def get_profile(
    profile_id: str, *, db: aiosqlite.Connection | None = None
) -> dict[str, Any] | None:
    close = db is None
    if db is None:
        db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM profiles WHERE id = ?", (profile_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None
    finally:
        if close:
            await db.close()


async def list_profiles(
    *, page: int = 1, page_size: int = 20
) -> tuple[list[dict[str, Any]], int]:
    db = await get_db()
    try:
        cursor = await db.execute("SELECT COUNT(*) FROM profiles")
        total = (await cursor.fetchone())[0]
        offset = (page - 1) * page_size
        cursor = await db.execute(
            "SELECT * FROM profiles ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (page_size, offset),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows], total
    finally:
        await db.close()


async def update_profile(profile_id: str, **fields: Any) -> dict[str, Any] | None:
    if not fields:
        return await get_profile(profile_id)
    invalid = set(fields) - _PROFILE_COLUMNS
    if invalid:
        raise ValueError(f"Invalid column names: {invalid}")
    fields["updated_at"] = _now()
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [profile_id]
    db = await get_db()
    try:
        await db.execute(
            f"UPDATE profiles SET {set_clause} WHERE id = ?", values
        )
        await db.commit()
        return await get_profile(profile_id, db=db)
    finally:
        await db.close()


async def delete_profile(profile_id: str) -> bool:
    db = await get_db()
    try:
        cursor = await db.execute("DELETE FROM profiles WHERE id = ?", (profile_id,))
        await db.commit()
        return cursor.rowcount > 0
    finally:
        await db.close()
