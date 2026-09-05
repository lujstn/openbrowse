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


async def test_repeat_stop_lets_the_first_teardown_finish(monkeypatch):
    """A second stop must not cancel a worker that is already unwinding, or it
    lands inside the teardown and abandons whatever it had left to hand back."""
    torn_down = asyncio.Event()

    async def fake_run(session_id: str) -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await asyncio.sleep(0.05)
            torn_down.set()
            raise

    monkeypatch.setattr(pool_mod, "run_agent_session", fake_run)
    p = SessionPool(max_concurrent=1)
    p.submit_nowait("s1")
    await asyncio.sleep(0)

    first = asyncio.ensure_future(p.cancel("s1"))
    await asyncio.sleep(0.01)
    second = asyncio.ensure_future(p.cancel("s1"))

    assert await first is True
    assert await second is True
    assert torn_down.is_set()
    assert p.active_count == 0


async def test_a_finished_worker_does_not_unregister_its_replacement(monkeypatch):
    finishes: list[asyncio.Event] = []

    async def fake_run(session_id: str) -> None:
        ev = asyncio.Event()
        finishes.append(ev)
        await ev.wait()

    monkeypatch.setattr(pool_mod, "run_agent_session", fake_run)
    p = SessionPool(max_concurrent=2)

    p.submit_nowait("s1")
    await asyncio.sleep(0)
    old = p._tasks["s1"]

    p.submit_nowait("s1")
    await asyncio.sleep(0)
    replacement = p._tasks["s1"]
    assert replacement is not old

    finishes[0].set()
    await asyncio.sleep(0.01)

    assert p._tasks.get("s1") is replacement
    assert p.active_count == 1
    assert await p.cancel("s1") is True
