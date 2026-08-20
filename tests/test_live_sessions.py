"""Tests for the live-session registry that keeps keep-alive sessions going."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.agent import live
from app.config import settings
from app.db import crud
from app.db.models import init_db


@pytest.fixture(autouse=True)
async def db(tmp_path, monkeypatch):
    test_settings = replace(
        settings,
        db_path=tmp_path / "test.db",
        data_dir=tmp_path / "data",
        profiles_dir=tmp_path / "data" / "profiles",
    )
    monkeypatch.setattr("app.config.settings", test_settings)
    monkeypatch.setattr("app.db.models.settings", test_settings)
    (tmp_path / "data" / "profiles").mkdir(parents=True)
    await init_db()
    yield test_settings
    live._live.clear()


def _agent() -> SimpleNamespace:
    stopped: list[bool] = []
    return SimpleNamespace(stop=lambda: stopped.append(True), stopped=stopped)


async def test_deliver_reports_cold_when_nothing_is_live():
    assert live.deliver("nobody", "hello") == live.COLD


async def test_deliver_reports_busy_while_a_task_is_running():
    live.register("s1", _agent())
    assert live.deliver("s1", "hello") == live.BUSY


async def test_deliver_hands_the_message_to_a_parked_worker():
    entry = live.register("s1", _agent())
    live.park(entry)

    assert live.deliver("s1", "and now the pricing page") == live.DELIVERED
    assert entry.inbox.get_nowait() == "and now the pricing page"
    # the worker is waking up, so a second follow-up must not slip in behind it
    assert not entry.parked
    assert live.deliver("s1", "one more") == live.BUSY


async def test_deliver_refuses_a_worker_that_is_being_released():
    entry = live.register("s1", _agent())
    live.park(entry)
    entry.release.set()

    assert live.deliver("s1", "hello") == live.BUSY


async def test_request_release_flags_the_worker_and_waits_for_teardown():
    entry = live.register("s1", _agent())

    async def worker():
        await entry.release.wait()
        live.unregister(entry)

    task = asyncio.create_task(worker())
    assert await live.request_release("s1", "stopped by test") is True
    await task
    assert entry.release_reason == "stopped by test"
    assert live.is_live("s1") is False


async def test_request_release_without_waiting_returns_immediately():
    entry = live.register("s1", _agent())
    assert await live.request_release("s1", "bye", wait=False) is True
    assert entry.release.is_set()
    assert live.is_live("s1") is True


async def test_request_release_gives_up_on_a_worker_that_never_finishes():
    live.register("s1", _agent())
    assert await live.request_release("s1", "bye", timeout=0.05) is True


async def test_release_idle_slot_takes_the_longest_parked_session():
    first = live.register("s1", _agent())
    second = live.register("s2", _agent())
    live.park(first)
    live.park(second)
    second.parked_since = first.parked_since.replace(year=first.parked_since.year + 1)

    async def worker(entry):
        await entry.release.wait()
        live.unregister(entry)

    task = asyncio.create_task(worker(first))
    assert await live.release_idle_slot("need the slot") is True
    await task
    assert first.release.is_set()
    assert not second.release.is_set()


async def test_release_idle_slot_leaves_busy_sessions_alone():
    entry = live.register("s1", _agent())
    assert await live.release_idle_slot("need the slot") is False
    assert not entry.release.is_set()


async def test_stop_agent_asks_the_agent_and_survives_a_failure():
    agent = _agent()
    live.register("s1", agent)
    assert live.stop_agent("s1") is True
    assert agent.stopped == [True]

    def boom():
        raise RuntimeError("event bus is gone")

    live.register("s2", SimpleNamespace(stop=boom))
    assert live.stop_agent("s2") is False
    assert live.stop_agent("s3") is False


async def test_unregister_only_clears_its_own_entry():
    old = live.register("s1", _agent())
    new = live.register("s1", _agent())
    live.unregister(old)

    assert live.get("s1") is new
    assert old.finished.is_set()


async def _turn(session_id: str, request: str, answer: str) -> None:
    await crud.create_message(
        session_id=session_id,
        role="user",
        msg_type="user_message",
        summary=request,
        count_step=False,
    )
    await crud.create_message(
        session_id=session_id,
        role="ai",
        msg_type="completion",
        summary="Task completed successfully",
        data=f'{{"output": "{answer}"}}',
        count_step=False,
    )


async def test_replay_preamble_is_empty_for_a_session_that_has_not_spoken():
    session = await crud.create_session(task="first", keep_alive=True)
    assert await live.replay_preamble(session["id"], "first") == ""


async def test_replay_preamble_carries_earlier_turns_in_order():
    session = await crud.create_session(task="news about Andy Burnham", keep_alive=True)
    await _turn(session["id"], "Summarise today's news about Andy Burnham", "He was made PM")
    await _turn(session["id"], "Which paper said that?", "The BBC")

    preamble = await live.replay_preamble(session["id"], "Is he really PM?")

    assert preamble.index("Andy Burnham") < preamble.index("Which paper")
    assert "He was made PM" in preamble
    assert preamble.rstrip().endswith("Is he really PM?")


async def test_replay_preamble_skips_the_request_being_served():
    session = await crud.create_session(task="t", keep_alive=True)
    await _turn(session["id"], "Summarise the news", "Here it is")
    # the follow-up is already on the feed when the runner replays context
    await crud.create_message(
        session_id=session["id"],
        role="user",
        msg_type="user_message",
        summary="Is he really PM?",
        count_step=False,
    )

    preamble = await live.replay_preamble(session["id"], "Is he really PM?")

    assert preamble.count("Is he really PM?") == 1
    assert "Summarise the news" in preamble


async def test_replay_preamble_drops_the_oldest_turns_over_the_cap():
    session = await crud.create_session(task="t", keep_alive=True)
    await _turn(session["id"], "the oldest question", "x" * 40)
    for i in range(8):
        await _turn(session["id"], f"question {i}", "y" * 1000)

    preamble = await live.replay_preamble(session["id"], "and now?")

    assert "the oldest question" not in preamble
    assert "question 7" in preamble
    assert len(preamble) < live._REPLAY_TOTAL_CAP + 1000


async def test_pool_submit_reclaims_the_slot_from_a_parked_session(monkeypatch):
    from app.agent import pool as pool_mod

    started: list[str] = []
    entries: dict[str, object] = {}

    async def fake_run(session_id: str) -> None:
        started.append(session_id)
        if session_id == "parked":
            entry = live.register("parked", SimpleNamespace())
            entries["parked"] = entry
            live.park(entry)
            await entry.release.wait()
            live.unregister(entry)

    monkeypatch.setattr(pool_mod, "run_agent_session", fake_run)
    sessions = pool_mod.SessionPool(max_concurrent=1)

    # the only slot is held by a session parked between follow-ups
    sessions.submit_nowait("parked")
    for _ in range(100):
        if "parked" in entries:
            break
        await asyncio.sleep(0.01)

    sessions.submit_nowait("fresh")
    for _ in range(100):
        if started == ["parked", "fresh"]:
            break
        await asyncio.sleep(0.01)

    assert entries["parked"].release.is_set()
    assert started == ["parked", "fresh"]
