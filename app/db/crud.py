"""CRUD operations for sessions, messages, and profiles."""

from __future__ import annotations

import json
import math
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import aiosqlite

from app.db.models import get_db

_SESSION_COLUMNS = {
    "status", "model", "task", "title", "output", "output_schema",
    "step_count", "last_step_summary", "is_task_successful", "live_url",
    "profile_id", "sensitive_data", "max_cost_usd", "default_max_cost_usd",
    "total_input_tokens", "total_output_tokens", "llm_cost_usd", "total_cost_usd",
    "capsolver_cost_usd",
    "screenshot_path", "display_num", "system_prompt_extension",
    "keep_alive", "reasoning_effort", "failure_kind", "failure_status_code", "updated_at",
}

_SAFE_PROFILE_ID = re.compile(r"^[A-Za-z0-9._-]+$")

_PROFILE_COLUMNS = {
    "name", "user_id", "storage_state_path", "last_used_at", "updated_at",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return str(uuid.uuid4())


# ── Sessions ──────────────────────────────────────────────────────────


def topped_up_budget(session: dict[str, Any]) -> float | None:
    """The budget a follow-up runs under when its caller named no new one.

    ``max_cost_usd`` caps the session's whole spend, so a conversation would
    strangle itself if every follow-up drew from one fixed pot. Each dispatch
    instead tops the pot back up by the allowance the session was created with,
    which bounds any single turn without ever bounding the conversation. A
    session created with no budget stays unbudgeted.
    """
    allowance = session.get("default_max_cost_usd")
    if not allowance:
        return None
    spent = float(session.get("total_cost_usd") or 0.0)
    return math.ceil((spent + float(allowance)) * 100 - 1e-9) / 100


async def create_session(
    *,
    task: str | None = None,
    model: str = "claude-sonnet-5",
    profile_id: str | None = None,
    output_schema: dict[str, Any] | None = None,
    sensitive_data: dict[str, str] | None = None,
    system_prompt_extension: str | None = None,
    max_cost_usd: float | None = None,
    default_max_cost_usd: float | None = None,
    keep_alive: bool = False,
    reasoning_effort: str = "default",
) -> dict[str, Any]:
    session_id = _new_id()
    now = _now()
    db = await get_db()
    try:
        await db.execute(
            """INSERT INTO sessions
               (id, status, model, task, profile_id, output_schema,
                sensitive_data, system_prompt_extension, max_cost_usd,
                default_max_cost_usd, keep_alive, reasoning_effort,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                default_max_cost_usd,
                int(keep_alive),
                reasoning_effort,
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


async def reconcile_interrupted_sessions() -> int:
    """Mark sessions orphaned by a server restart as errored.

    Call once at startup, when the pool is provably empty: any session still
    marked 'running', or 'created' with a task it never got to submit, was
    interrupted mid-flight and can never resume, so its status is a lie.
    """
    now = _now()
    db = await get_db()
    try:
        cursor = await db.execute(
            """UPDATE sessions
               SET status = 'error',
                   last_step_summary = 'Interrupted by server restart',
                   updated_at = ?
               WHERE status = 'running'
                  OR (status = 'created' AND task IS NOT NULL)""",
            (now,),
        )
        await db.commit()
        return cursor.rowcount
    finally:
        await db.close()


async def expire_stale_sessions(older_than_minutes: int) -> int:
    """Expire task-less 'created' shells that were never given work.

    A session created via /v3/sessions with no task never reaches the pool, so
    it sits at 'created' forever and renders as a perpetually-ongoing row. Once
    older than the TTL it is a dead shell and is moved to a terminal state.
    """
    cutoff = (
        datetime.now(timezone.utc) - timedelta(minutes=older_than_minutes)
    ).isoformat()
    now = _now()
    db = await get_db()
    try:
        cursor = await db.execute(
            """UPDATE sessions
               SET status = 'expired',
                   last_step_summary = 'Expired: created without a task',
                   updated_at = ?
               WHERE status = 'created' AND task IS NULL AND created_at < ?""",
            (now, cutoff),
        )
        await db.commit()
        return cursor.rowcount
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
    count_step: bool = True,
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
        if count_step:
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


async def upsert_profile(
    profile_id: str, *, name: str | None = None
) -> dict[str, Any] | None:
    """Create a profile with an explicit id, or update its name if it already exists.

    Unlike create_profile (which mints a uuid), this keys on a caller-supplied id — used by
    cookie import so a local profile matches its BU Cloud id. A None name never clears an
    existing name.
    """
    profile_id = (profile_id or "").strip()
    if not profile_id or ".." in profile_id or not _SAFE_PROFILE_ID.match(profile_id):
        raise ValueError("profile id may only contain letters, digits, dot, dash and underscore")
    now = _now()
    storage_path = f"profiles/{profile_id}.json"
    db = await get_db()
    try:
        cursor = await db.execute("SELECT 1 FROM profiles WHERE id = ?", (profile_id,))
        exists = await cursor.fetchone() is not None
        if exists:
            if name is not None:
                await db.execute(
                    "UPDATE profiles SET name = ?, updated_at = ? WHERE id = ?",
                    (name, now, profile_id),
                )
                await db.commit()
        else:
            await db.execute(
                """INSERT INTO profiles (id, name, user_id, storage_state_path, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (profile_id, name, None, storage_path, now, now),
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
    *, page: int = 1, page_size: int = 20, query: str | None = None
) -> tuple[list[dict[str, Any]], int]:
    db = await get_db()
    try:
        where = ""
        params: list[Any] = []
        if query:
            where = "WHERE name LIKE ?"
            params.append(f"%{query}%")
        cursor = await db.execute(f"SELECT COUNT(*) FROM profiles {where}", params)
        total = (await cursor.fetchone())[0]
        offset = (page - 1) * page_size
        cursor = await db.execute(
            f"SELECT * FROM profiles {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            params + [page_size, offset],
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


async def rename_profile(old_id: str, new_id: str) -> dict[str, Any] | None:
    new_id = (new_id or "").strip()
    if not new_id:
        raise ValueError("new profile id must not be empty")
    if ".." in new_id or not _SAFE_PROFILE_ID.match(new_id):
        raise ValueError("profile id may only contain letters, digits, dot, dash and underscore")
    if new_id == old_id:
        return await get_profile(old_id)
    now = _now()
    db = await get_db()
    try:
        cursor = await db.execute("SELECT 1 FROM profiles WHERE id = ?", (new_id,))
        if await cursor.fetchone():
            raise ValueError(f"profile id {new_id} already exists")
        cursor = await db.execute("SELECT 1 FROM profiles WHERE id = ?", (old_id,))
        if not await cursor.fetchone():
            return None
        await db.execute("PRAGMA defer_foreign_keys = ON")
        await db.execute(
            "UPDATE profiles SET id = ?, storage_state_path = ?, updated_at = ? WHERE id = ?",
            (new_id, f"profiles/{new_id}.json", now, old_id),
        )
        await db.execute(
            "UPDATE sessions SET profile_id = ? WHERE profile_id = ?",
            (new_id, old_id),
        )
        await db.commit()
        return await get_profile(new_id, db=db)
    finally:
        await db.close()


async def delete_profile_cascade(profile_id: str) -> bool:
    db = await get_db()
    try:
        await db.execute(
            "UPDATE sessions SET profile_id = NULL WHERE profile_id = ?", (profile_id,)
        )
        cursor = await db.execute("DELETE FROM profiles WHERE id = ?", (profile_id,))
        await db.commit()
        return cursor.rowcount > 0
    finally:
        await db.close()
