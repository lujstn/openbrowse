"""Many sessions, one profile, two slots — queueing, hand-off and lost-update safety.

Exercises the runner's real state helpers rather than a stand-in, so what the test
proves about ordering is what the runner actually does.
"""

import asyncio
from dataclasses import replace

import pytest

import openbrowse.agent.pool as pool_mod
from openbrowse.agent import runner
from openbrowse.agent.pool import SessionPool
from openbrowse.config import settings
from openbrowse.profiles import merge

REQUESTS = 1000
SLOTS = 2


@pytest.fixture
def profile_file(tmp_path, monkeypatch):
    test_settings = replace(settings, data_dir=tmp_path, profiles_dir=tmp_path / "profiles")
    monkeypatch.setattr(runner, "settings", test_settings)
    runner._storage_locks.clear()
    path = tmp_path / "profiles" / "p1.json"
    merge.write_state(path, {"cookies": [], "origins": []})
    return path


def _cookie(name, value):
    return {"name": name, "value": value, "domain": "example.com", "path": "/"}


class _Harness:
    """Runs the runner's own open/merge pair around a body that mutates the jar."""

    def __init__(self, profile_file):
        self.profile_file = profile_file
        self.live = 0
        self.peak = 0
        self.ran: list[str] = []
        self.merged: set[str] = set()
        self.seen_at_open: dict[str, set[str]] = {}
        self.missed_handoff: list[tuple[str, set[str]]] = []

    async def run(self, session_id: str) -> None:
        self.live += 1
        self.peak = max(self.peak, self.live)
        self.ran.append(session_id)
        try:
            settled = set(self.merged)
            working, baseline = await runner._open_session_state(session_id, self.profile_file)
            names = {c["name"] for c in (baseline or {}).get("cookies", [])}
            self.seen_at_open[session_id] = names
            # Every session that finished merging before this one opened its copy
            # must be visible in the baseline it starts from.
            if not settled <= names:
                self.missed_handoff.append((session_id, settled - names))

            await asyncio.sleep(0)
            state = merge.read_state(working)
            state["cookies"].append(_cookie(session_id, "1"))
            merge.write_state(working, state)
            await asyncio.sleep(0)

            await runner._merge_state_into_profile(self.profile_file, working, baseline)
            self.merged.add(session_id)
        finally:
            self.live -= 1


async def test_a_thousand_requests_on_one_profile_queue_behind_two_slots(
    profile_file, monkeypatch
):
    harness = _Harness(profile_file)
    monkeypatch.setattr(pool_mod, "run_agent_session", harness.run)
    p = SessionPool(max_concurrent=SLOTS)

    ids = [f"s{i:04d}" for i in range(REQUESTS)]
    for session_id in ids:
        p.submit_nowait(session_id)

    # Submitting is non-blocking: nothing has run yet, and everything is queued.
    assert p.active_count == 0
    assert p.queued_count == REQUESTS

    await asyncio.gather(*[p._tasks[i] for i in ids if i in p._tasks])

    assert harness.peak == SLOTS, f"ran {harness.peak} at once, expected {SLOTS}"
    assert sorted(harness.ran) == sorted(ids)
    assert p.active_count == 0
    assert p.queued_count == 0


async def test_no_session_loses_its_cookies_to_a_concurrent_one(profile_file, monkeypatch):
    harness = _Harness(profile_file)
    monkeypatch.setattr(pool_mod, "run_agent_session", harness.run)
    p = SessionPool(max_concurrent=SLOTS)

    ids = [f"s{i:04d}" for i in range(REQUESTS)]
    for session_id in ids:
        p.submit_nowait(session_id)
    await asyncio.gather(*[p._tasks[i] for i in ids if i in p._tasks])

    final = merge.read_state(profile_file)
    written = {c["name"] for c in final["cookies"]}
    assert written == set(ids), f"lost {len(set(ids) - written)} session(s) of cookies"


async def test_a_finished_session_is_visible_to_every_later_one(profile_file, monkeypatch):
    harness = _Harness(profile_file)
    monkeypatch.setattr(pool_mod, "run_agent_session", harness.run)
    p = SessionPool(max_concurrent=SLOTS)

    ids = [f"s{i:04d}" for i in range(REQUESTS)]
    for session_id in ids:
        p.submit_nowait(session_id)
    await asyncio.gather(*[p._tasks[i] for i in ids if i in p._tasks])

    assert harness.missed_handoff == []
    # The profile really does grow as the queue drains, rather than every session
    # starting from the empty jar the first one saw.
    assert harness.seen_at_open[ids[0]] == set()
    assert len(harness.seen_at_open[ids[-1]]) >= REQUESTS - SLOTS


async def test_serial_slots_hand_over_the_whole_jar(profile_file, monkeypatch):
    harness = _Harness(profile_file)
    monkeypatch.setattr(pool_mod, "run_agent_session", harness.run)
    p = SessionPool(max_concurrent=1)

    ids = [f"s{i:03d}" for i in range(50)]
    for session_id in ids:
        p.submit_nowait(session_id)
    await asyncio.gather(*[p._tasks[i] for i in ids if i in p._tasks])

    assert harness.peak == 1
    for index, session_id in enumerate(ids):
        assert harness.seen_at_open[session_id] == set(ids[:index])


async def test_working_copies_do_not_outlive_their_sessions(profile_file, monkeypatch):
    harness = _Harness(profile_file)
    monkeypatch.setattr(pool_mod, "run_agent_session", harness.run)
    p = SessionPool(max_concurrent=SLOTS)

    ids = [f"s{i:03d}" for i in range(20)]
    for session_id in ids:
        p.submit_nowait(session_id)
    await asyncio.gather(*[p._tasks[i] for i in ids if i in p._tasks])

    leftovers = list((profile_file.parent.parent / "session-state").glob("*.json"))
    assert leftovers == []


async def test_a_crashed_run_leaves_the_profile_untouched(profile_file):
    merge.write_state(profile_file, {"cookies": [_cookie("keep", "me")], "origins": []})
    working, baseline = await runner._open_session_state("crashed", profile_file)
    # The browser never wrote its jar back, so the copy still holds the baseline.
    await runner._merge_state_into_profile(profile_file, working, baseline)
    assert merge.read_state(profile_file)["cookies"] == [_cookie("keep", "me")]


async def test_stale_working_copies_are_cleared_at_startup(profile_file, monkeypatch):
    working, _ = await runner._open_session_state("orphan", profile_file)
    assert working.exists()
    runner.clear_session_states()
    assert not working.exists()
