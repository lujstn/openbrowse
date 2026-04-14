"""SQLite database schema and connection management."""

from __future__ import annotations

import aiosqlite

from app.config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'created',
    model TEXT NOT NULL DEFAULT 'claude-sonnet-4-6',
    task TEXT,
    title TEXT,
    output TEXT,
    output_schema TEXT,
    step_count INTEGER NOT NULL DEFAULT 0,
    last_step_summary TEXT,
    is_task_successful INTEGER,
    live_url TEXT,
    profile_id TEXT,
    sensitive_data TEXT,
    max_cost_usd REAL,
    total_input_tokens INTEGER NOT NULL DEFAULT 0,
    total_output_tokens INTEGER NOT NULL DEFAULT 0,
    llm_cost_usd REAL NOT NULL DEFAULT 0.0,
    total_cost_usd REAL NOT NULL DEFAULT 0.0,
    screenshot_path TEXT,
    display_num INTEGER,
    system_prompt_extension TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (profile_id) REFERENCES profiles(id)
);

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'ai',
    data TEXT NOT NULL DEFAULT '',
    type TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    screenshot_path TEXT,
    hidden INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_messages_session_created
    ON messages(session_id, created_at);

CREATE TABLE IF NOT EXISTS profiles (
    id TEXT PRIMARY KEY,
    name TEXT,
    user_id TEXT,
    storage_state_path TEXT,
    last_used_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


async def get_db() -> aiosqlite.Connection:
    """Get a database connection. Caller must close it."""
    db = await aiosqlite.connect(str(settings.db_path))
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    return db


async def init_db() -> None:
    """Initialize the database schema."""
    db = await get_db()
    try:
        await db.executescript(SCHEMA)
        await db.commit()
    finally:
        await db.close()
