"""Many sessions, one profile, two slots: queueing, hand-off and lost-update safety.

Each session runs the real profile sync against its own in-memory browser, so what the
test proves about ordering is what a session actually does.
"""

import asyncio
import json

import pytest

import openbrowse.agent.pool as pool_mod
from openbrowse.agent.pool import SessionPool
from openbrowse.profiles import store, sync
from openbrowse.profiles.sync import ProfileSync
from tests.fake_chrome import FakeChrome

REQUESTS = 1000
SLOTS = 2


@pytest.fixture
def profile_file(tmp_path, monkeypatch):
    browsers: dict[object, FakeChrome] = {}

    async def open_fake(cdp_url):
        return browsers.setdefault(cdp_url, FakeChrome())

    monkeypatch.setattr(sync.CdpConnection, "open", staticmethod(open_fake))
    monkeypatch.setattr(sync, "CHECKPOINT_S", 3600.0)
    path = tmp_path / "profiles" / "p1.json"
    store.save(path, store.ProfileData())
    return path


def _cookie_names(path):
    return {c["name"] for c in json.loads(path.read_text())["cookies"]}


class _Harness:
    """Restores the profile into a fresh browser, signs in to one more site, writes back."""

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
            cdp_url = object()
            session = ProfileSync(self.profile_file, cdp_url, label=session_id)
            await session.restore()
            chrome = await sync.CdpConnection.open(cdp_url)
            names = set(chrome.jar())
            self.seen_at_open[session_id] = names
            # Every session that finished writing back before this one restored
            # must be in the browser it starts with.
            if not settled <= names:
                self.missed_handoff.append((session_id, settled - names))

            await asyncio.sleep(0)
            chrome.site_sets_cookie(session_id, "1", "example.com")
            await asyncio.sleep(0)

            await session.write_back()
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

    written = _cookie_names(profile_file)
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


async def test_a_session_that_changed_nothing_leaves_the_profile_as_it_was(profile_file):
    store.save(
        profile_file,
        store.ProfileData({"cookies": [{"name": "keep", "value": "me", "domain": "example.com", "path": "/"}]}),
    )
    before = json.loads(profile_file.read_text())
    session = ProfileSync(profile_file, object())
    await session.restore()
    await session.write_back()
    assert json.loads(profile_file.read_text()) == before
