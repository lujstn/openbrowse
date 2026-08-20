"""SessionPool tests — semaphore gating, non-blocking submits, permit integrity."""

import asyncio


import openbrowse.agent.pool as pool_mod
from openbrowse.agent.pool import SessionPool


def _gated_runner(monkeypatch):
    """Replace run_agent_session with one that blocks until told to finish,
    recording which sessions actually ran."""
    started: list[str] = []
    releases: dict[str, asyncio.Event] = {}

    async def fake_run(session_id: str) -> None:
        started.append(session_id)
        ev = releases.setdefault(session_id, asyncio.Event())
        await ev.wait()

    monkeypatch.setattr(pool_mod, "run_agent_session", fake_run)
    return started, releases


async def test_submit_nowait_returns_while_full(monkeypatch):
    started, releases = _gated_runner(monkeypatch)
    p = SessionPool(max_concurrent=1)

    p.submit_nowait("s1")
    await asyncio.sleep(0)
    assert started == ["s1"]
    assert p.active_count == 1

    p.submit_nowait("s2")
    await asyncio.sleep(0)
    assert started == ["s1"]
    assert p.active_count == 1
    assert p.queued_count == 1

    releases.setdefault("s1", asyncio.Event()).set()
    await asyncio.sleep(0.01)
    assert started == ["s1", "s2"]
    assert p.active_count == 1
    assert p.queued_count == 0

    releases.setdefault("s2", asyncio.Event()).set()
    await asyncio.sleep(0.01)
    assert p.active_count == 0


async def test_cancel_queued_session_leaks_no_permit(monkeypatch):
    started, releases = _gated_runner(monkeypatch)
    p = SessionPool(max_concurrent=1)

    p.submit_nowait("s1")
    p.submit_nowait("s2")
    await asyncio.sleep(0)
    assert started == ["s1"]

    assert await p.cancel("s2") is True
    releases.setdefault("s1", asyncio.Event()).set()
    await asyncio.sleep(0.01)
    assert started == ["s1"]

    p.submit_nowait("s3")
    releases.setdefault("s3", asyncio.Event()).set()
    await asyncio.sleep(0.01)
    assert started == ["s1", "s3"]
    assert p.active_count == 0


async def test_cancel_running_session_releases_slot(monkeypatch):
    started, releases = _gated_runner(monkeypatch)
    p = SessionPool(max_concurrent=1)

    p.submit_nowait("s1")
    await asyncio.sleep(0)
    assert p.active_count == 1

    assert await p.cancel("s1") is True
    assert p.active_count == 0

    p.submit_nowait("s2")
    releases.setdefault("s2", asyncio.Event()).set()
    await asyncio.sleep(0.01)
    assert started == ["s1", "s2"]


async def test_runner_exception_releases_slot(monkeypatch):
    async def boom(session_id: str) -> None:
        raise RuntimeError("kaput")

    monkeypatch.setattr(pool_mod, "run_agent_session", boom)
    p = SessionPool(max_concurrent=1)
    p.submit_nowait("s1")
    await asyncio.sleep(0.01)
    assert p.active_count == 0
    assert p.queued_count == 0

    started, releases = _gated_runner(monkeypatch)
    p.submit_nowait("s2")
    await asyncio.sleep(0)
    assert started == ["s2"]
    releases.setdefault("s2", asyncio.Event()).set()
    await asyncio.sleep(0.01)


async def test_shutdown_cancels_running_and_queued(monkeypatch):
    started, releases = _gated_runner(monkeypatch)
    p = SessionPool(max_concurrent=1)
    p.submit_nowait("s1")
    p.submit_nowait("s2")
    await asyncio.sleep(0)

    await p.shutdown()
    assert p.active_count == 0
    assert p.queued_count == 0
