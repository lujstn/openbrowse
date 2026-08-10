"""Tests for database CRUD operations."""

from dataclasses import replace

import pytest

from app.config import settings
from app.db.models import init_db
from app.db import crud


@pytest.fixture(autouse=True)
async def setup_db(tmp_path, monkeypatch):
    """Use a temp database for each test."""
    test_settings = replace(settings, db_path=tmp_path / "test.db")
    monkeypatch.setattr("app.db.models.settings", test_settings)
    monkeypatch.setattr("app.config.settings", test_settings)
    await init_db()


async def test_create_and_get_session():
    session = await crud.create_session(task="test task", model="claude-sonnet-4-6")
    assert session["task"] == "test task"
    assert session["status"] == "created"
    fetched = await crud.get_session(session["id"])
    assert fetched is not None
    assert fetched["id"] == session["id"]


async def test_list_sessions():
    await crud.create_session(task="task 1")
    await crud.create_session(task="task 2")
    sessions, total = await crud.list_sessions()
    assert total == 2
    assert len(sessions) == 2


async def test_update_session():
    session = await crud.create_session(task="test")
    updated = await crud.update_session(session["id"], status="running")
    assert updated["status"] == "running"


async def test_create_and_list_messages():
    session = await crud.create_session(task="test")
    await crud.create_message(
        session_id=session["id"], summary="Navigating to google.com", msg_type="browser_action"
    )
    await crud.create_message(
        session_id=session["id"], summary="Clicking element", msg_type="browser_action"
    )
    messages, has_more = await crud.list_messages(session["id"])
    assert len(messages) == 2
    assert not has_more


async def test_reconcile_interrupted_sessions():
    running = await crud.create_session(task="live task")
    await crud.update_session(running["id"], status="running")
    queued = await crud.create_session(task="queued task")
    shell = await crud.create_session()
    done = await crud.create_session(task="done")
    await crud.update_session(done["id"], status="stopped")

    reconciled = await crud.reconcile_interrupted_sessions()
    assert reconciled == 2

    assert (await crud.get_session(running["id"]))["status"] == "error"
    assert (await crud.get_session(queued["id"]))["status"] == "error"
    assert (await crud.get_session(shell["id"]))["status"] == "created"
    assert (await crud.get_session(done["id"]))["status"] == "stopped"


async def test_expire_stale_sessions_only_taskless_created():
    shell = await crud.create_session()
    with_task = await crud.create_session(task="has work")

    expired = await crud.expire_stale_sessions(older_than_minutes=-1)
    assert expired == 1
    assert (await crud.get_session(shell["id"]))["status"] == "expired"
    assert (await crud.get_session(with_task["id"]))["status"] == "created"


async def test_expire_stale_sessions_spares_fresh_shells():
    await crud.create_session()
    expired = await crud.expire_stale_sessions(older_than_minutes=15)
    assert expired == 0


async def test_create_and_get_profile():
    profile = await crud.create_profile(name="Test Profile")
    assert profile["name"] == "Test Profile"
    fetched = await crud.get_profile(profile["id"])
    assert fetched is not None


async def test_delete_profile():
    profile = await crud.create_profile(name="To Delete")
    assert await crud.delete_profile(profile["id"])
    assert await crud.get_profile(profile["id"]) is None
