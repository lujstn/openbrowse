"""A session's profile, restored into a fresh browser and written back out of it."""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from openbrowse.profiles import seed, store, sync
from openbrowse.profiles.sync import ProfileSync
from tests.fake_chrome import FakeChrome


@pytest.fixture
def browsers(monkeypatch):
    opened: list[FakeChrome] = []

    async def open_fake(cdp_url):
        chrome = FakeChrome()
        opened.append(chrome)
        return chrome

    monkeypatch.setattr(sync.CdpConnection, "open", staticmethod(open_fake))
    monkeypatch.setattr(sync, "CHECKPOINT_S", 3600.0)
    return opened


@pytest.fixture
def profile(tmp_path):
    path = tmp_path / "profiles" / "p1.json"
    store.save(path, store.ProfileData())
    return path


def _write(path, cookies=(), origins=None, last_used=None):
    state = {"cookies": list(cookies), "origins": [
        {"origin": o, "localStorage": [{"name": k, "value": v} for k, v in items.items()]}
        for o, items in (origins or {}).items()
    ]}
    store.save(path, store.ProfileData(state, dict(last_used or {})))


def _cookie(name, value, domain="shop.example", **extra):
    return {"name": name, "value": value, "domain": domain, "path": "/", **extra}


def _read(path):
    return json.loads(path.read_text())


def _jar(path):
    return {c["name"]: c["value"] for c in _read(path)["cookies"]}


def _storage(path):
    return {
        e["origin"]: {i["name"]: i["value"] for i in e["localStorage"]}
        for e in _read(path)["origins"]
    }


async def test_restore_puts_each_sites_data_in_its_own_storage(profile, browsers):
    _write(
        profile,
        cookies=[_cookie("sid", "1"), _cookie("pref", "dark", expires=-1)],
        origins={
            "https://shop.example": {"token": "abc"},
            "https://bank.example": {"session": "xyz"},
        },
    )
    restored = await ProfileSync(profile, "fake").restore()
    chrome = browsers[0]

    assert chrome.jar() == {"sid": "1", "pref": "dark"}
    assert chrome.storage == {
        "https://shop.example": {"token": "abc"},
        "https://bank.example": {"session": "xyz"},
    }
    assert restored.cookies == 2 and restored.sites == 2 and not restored.site_failures
    assert chrome.network == []
    assert chrome.open_seed_tabs == 0
    assert list(chrome.targets) == ["initial"]


async def test_restore_spreads_sites_over_at_most_four_tabs(profile, browsers):
    _write(profile, origins={f"https://s{i}.example": {"k": str(i)} for i in range(12)})
    await ProfileSync(profile, "fake").restore()
    chrome = browsers[0]
    assert len(chrome.storage) == 12
    assert 1 < chrome.peak_seed_tabs <= 4


async def test_expired_cookies_and_long_idle_sites_are_not_restored(profile, browsers):
    now = time.time()
    _write(
        profile,
        cookies=[_cookie("old", "1", expires=now - 60), _cookie("live", "2", expires=now + 3600)],
        origins={"https://idle.example": {"k": "v"}, "https://fresh.example": {"k": "v"}},
        last_used={"https://idle.example": now - 31 * 86400, "https://fresh.example": now},
    )
    await ProfileSync(profile, "fake").restore()
    chrome = browsers[0]
    assert chrome.jar() == {"live": "2"}
    assert list(chrome.storage) == ["https://fresh.example"]


async def test_a_session_writes_back_what_it_changed(profile, browsers):
    _write(profile, cookies=[_cookie("sid", "old")], origins={"https://shop.example": {"cart": "1"}})
    session = ProfileSync(profile, "fake")
    await session.restore()
    chrome = browsers[0]

    chrome.visit("https://shop.example/basket")
    chrome.site_sets_cookie("sid", "new", "shop.example")
    chrome.storage["https://shop.example"]["cart"] = "2"
    await session.write_back()

    assert _jar(profile) == {"sid": "new"}
    assert _storage(profile) == {"https://shop.example": {"cart": "2"}}
    assert chrome.closed
    meta = json.loads(store.meta_path(profile).read_text())
    assert meta["origins"]["https://shop.example"]["lastUsed"] == pytest.approx(time.time(), abs=5)


async def test_chromes_own_cookie_fields_do_not_count_as_a_change(profile, browsers):
    _write(profile, cookies=[_cookie("sid", "old")])
    session = ProfileSync(profile, "fake")
    await session.restore()
    _write(profile, cookies=[_cookie("sid", "from-another-session")])
    await session.write_back()
    assert _jar(profile) == {"sid": "from-another-session"}


async def test_a_site_the_session_never_opened_keeps_the_profiles_value(profile, browsers):
    _write(profile, origins={"https://shop.example": {"cart": "1"}})
    session = ProfileSync(profile, "fake")
    await session.restore()
    _write(profile, origins={"https://shop.example": {"cart": "changed-elsewhere"}})
    await session.write_back()
    assert _storage(profile) == {"https://shop.example": {"cart": "changed-elsewhere"}}


async def test_a_site_emptied_by_the_session_is_emptied_in_the_profile(profile, browsers):
    _write(profile, origins={"https://shop.example": {"token": "t"}})
    session = ProfileSync(profile, "fake")
    await session.restore()
    chrome = browsers[0]
    chrome.visit("https://shop.example/logout")
    chrome.storage["https://shop.example"].clear()
    await session.write_back()
    assert _storage(profile) == {}


async def test_new_sites_the_session_signed_in_to_are_kept(profile, browsers):
    session = ProfileSync(profile, "fake")
    await session.restore()
    chrome = browsers[0]
    chrome.visit("https://new.example/login")
    chrome.storage["https://new.example"] = {"jwt": "j"}
    chrome.site_sets_cookie("auth", "a", ".new.example")
    await session.write_back()
    assert _jar(profile) == {"auth": "a"}
    assert _storage(profile) == {"https://new.example": {"jwt": "j"}}


async def test_a_cookie_chrome_refused_is_not_erased_from_the_profile(profile, browsers, monkeypatch):
    _write(profile, cookies=[_cookie("odd", "1"), _cookie("ok", "2")])
    original = FakeChrome.__init__

    def refusing(self):
        original(self)
        self.refuse_cookie_names = {"odd"}

    monkeypatch.setattr(FakeChrome, "__init__", refusing)
    session = ProfileSync(profile, "fake")
    restored = await session.restore()
    assert restored.cookies == 1 and restored.cookie_failures == 1
    await session.write_back()
    assert _jar(profile) == {"odd": "1", "ok": "2"}


async def test_a_site_that_failed_to_restore_is_left_alone(profile, browsers, monkeypatch):
    _write(profile, origins={"https://broken.example": {"k": "v"}, "https://ok.example": {"k": "v"}})
    original = FakeChrome.__init__

    def failing(self):
        original(self)
        self.fail_origins = {"https://broken.example"}

    monkeypatch.setattr(FakeChrome, "__init__", failing)
    session = ProfileSync(profile, "fake")
    restored = await session.restore()
    assert list(restored.site_failures) == ["https://broken.example"]
    assert restored.sites == 1
    browsers[0].visit("https://broken.example/")
    await session.write_back()
    assert _storage(profile) == {"https://broken.example": {"k": "v"}, "https://ok.example": {"k": "v"}}


async def test_restore_gives_up_on_sites_past_its_deadline(profile, browsers, monkeypatch):
    _write(profile, origins={"https://slow.example": {"k": "v"}})
    monkeypatch.setattr(sync, "_RESTORE_DEADLINE_S", 0.2)
    original = FakeChrome.__init__

    def slow(self):
        original(self)
        self.slow_origins = {"https://slow.example": 5.0}

    monkeypatch.setattr(FakeChrome, "__init__", slow)
    session = ProfileSync(profile, "fake")
    restored = await session.restore()
    assert restored.sites == 0 and "https://slow.example" in restored.site_failures
    browsers[0].visit("https://slow.example/")
    await session.write_back()
    assert _storage(profile) == {"https://slow.example": {"k": "v"}}


async def test_the_sessions_own_blank_pages_are_not_counted_as_visits(profile, browsers):
    _write(profile, origins={"https://shop.example": {"k": "v"}})
    session = ProfileSync(profile, "fake")
    await session.restore()
    await asyncio.sleep(0)
    assert session.visited == set()


async def test_a_checkpoint_writes_cookies_only_when_they_change(profile, browsers):
    _write(profile, cookies=[_cookie("sid", "1")])
    session = ProfileSync(profile, "fake")
    await session.restore()
    chrome = browsers[0]
    assert await session.checkpoint() is False
    chrome.site_sets_cookie("sid", "2", "shop.example")
    assert await session.checkpoint() is True
    assert _jar(profile) == {"sid": "2"}
    assert await session.checkpoint() is False
    await session.write_back()


async def test_a_checkpoint_does_not_let_the_session_roll_back_a_later_change(profile, browsers):
    _write(profile, cookies=[_cookie("sid", "1")])
    session = ProfileSync(profile, "fake")
    await session.restore()
    browsers[0].site_sets_cookie("sid", "2", "shop.example")
    await session.checkpoint()
    _write(profile, cookies=[_cookie("sid", "3-from-another-session")])
    await session.write_back()
    assert _jar(profile) == {"sid": "3-from-another-session"}


async def test_a_deleted_profile_is_not_brought_back(profile, browsers):
    session = ProfileSync(profile, "fake")
    await session.restore()
    browsers[0].site_sets_cookie("sid", "1", "shop.example")
    store.remove(profile)
    await session.write_back()
    assert not profile.exists()


async def test_two_sessions_on_one_profile_keep_each_others_changes(profile, browsers):
    _write(profile, cookies=[_cookie("shared", "0")])
    first, second = ProfileSync(profile, "fake"), ProfileSync(profile, "fake")
    await asyncio.gather(first.restore(), second.restore())
    a, b = browsers
    a.visit("https://a.example/")
    a.storage["https://a.example"] = {"who": "a"}
    a.site_sets_cookie("a", "1", "a.example")
    b.visit("https://b.example/")
    b.storage["https://b.example"] = {"who": "b"}
    b.site_sets_cookie("b", "1", "b.example")
    await asyncio.gather(first.write_back(), second.write_back())
    assert _jar(profile) == {"shared": "0", "a": "1", "b": "1"}
    assert _storage(profile) == {"https://a.example": {"who": "a"}, "https://b.example": {"who": "b"}}


async def test_abandoning_writes_nothing(profile, browsers):
    _write(profile, cookies=[_cookie("sid", "1")])
    session = ProfileSync(profile, "fake")
    await session.restore()
    browsers[0].site_sets_cookie("sid", "2", "shop.example")
    await session.abandon()
    assert _jar(profile) == {"sid": "1"}
    assert browsers[0].closed


async def test_visiting_origins_carries_on_past_one_that_fails():
    chrome = FakeChrome()
    chrome.fail_origins = {"https://b.example"}
    progress = []
    visit = await seed.visit_origins(
        chrome,
        ["https://a.example", "https://b.example", "https://c.example"],
        seed.write_local_storage([{"name": "k", "value": "v"}]),
        tabs=1,
        on_progress=lambda done, total: progress.append((done, total)),
    )
    assert set(visit.results) == {"https://a.example", "https://c.example"}
    assert list(visit.failures) == ["https://b.example"]
    assert progress == [(1, 3), (2, 3), (3, 3)]
    assert chrome.open_seed_tabs == 0 and chrome.network == []


async def test_stored_values_survive_json_and_script_quoting():
    chrome = FakeChrome()
    tricky = {"name": 'k"}\n', "value": "</script> '\"${x}` \\"}
    await seed.visit_origins(chrome, ["https://a.example"], seed.write_local_storage([tricky]))
    read = await seed.visit_origins(chrome, ["https://a.example"], seed.read_local_storage)
    assert read.results["https://a.example"] == [tricky]


async def test_visiting_waits_for_chrome_to_finish_closing_its_tabs(monkeypatch):
    chrome = FakeChrome()
    closing: dict[str, dict] = {}
    original = chrome._Target_closeTarget

    async def close_later(params, session_id):
        closing[params["targetId"]] = chrome.targets[params["targetId"]]
        reply = await original(params, session_id)
        chrome.targets[params["targetId"]] = closing[params["targetId"]]

        async def finish():
            await asyncio.sleep(0.2)
            chrome.targets.pop(params["targetId"], None)

        asyncio.ensure_future(finish())
        return reply

    monkeypatch.setattr(chrome, "_Target_closeTarget", close_later)
    await seed.visit_origins(chrome, ["https://a.example"], seed.read_local_storage)
    assert list(chrome.targets) == ["initial"]


async def test_a_session_stopped_during_restore_writes_nothing_back(profile, browsers, monkeypatch):
    _write(profile, cookies=[_cookie("sid", "1")], origins={"https://slow.example": {"k": "v"}})
    original = FakeChrome.__init__

    def slow(self):
        original(self)
        self.slow_origins = {"https://slow.example": 5.0}

    monkeypatch.setattr(FakeChrome, "__init__", slow)
    session = ProfileSync(profile, "fake")
    restoring = asyncio.create_task(session.restore())
    await asyncio.sleep(0.1)
    restoring.cancel()
    with pytest.raises(asyncio.CancelledError):
        await restoring
    _write(profile, cookies=[_cookie("sid", "2-from-another-session")])
    await session.write_back()
    assert _jar(profile) == {"sid": "2-from-another-session"}
    assert browsers[0].closed


async def test_the_site_a_session_used_survives_the_first_write_to_an_unrecorded_profile(
    profile, browsers
):
    big = "x" * 30_000
    profile.write_text(json.dumps({
        "cookies": [],
        "origins": [
            {"origin": f"https://s{i}.example", "localStorage": [{"name": "k", "value": big}]}
            for i in range(120)
        ] + [{"origin": "https://used.example", "localStorage": [{"name": "k", "value": big * 2}]}],
    }))
    store.meta_path(profile).unlink()
    session = ProfileSync(profile, "fake")
    await session.restore()
    chrome = browsers[0]
    chrome.visit("https://used.example/")
    chrome.storage.setdefault("https://used.example", {})["probe"] = "kept"
    await session.write_back()
    assert _storage(profile)["https://used.example"]["probe"] == "kept"


async def test_the_site_a_session_used_survives_a_second_sessions_write(profile, browsers):
    big = "x" * 30_000
    profile.write_text(json.dumps({
        "cookies": [],
        "origins": [
            {"origin": f"https://s{i}.example", "localStorage": [{"name": "k", "value": big}]}
            for i in range(120)
        ] + [
            {"origin": "https://a.example", "localStorage": [{"name": "k", "value": big * 3}]},
            {"origin": "https://b.example", "localStorage": [{"name": "k", "value": big}]},
        ],
    }))
    store.meta_path(profile).unlink()
    first, second = ProfileSync(profile, "fake"), ProfileSync(profile, "fake")
    await asyncio.gather(first.restore(), second.restore())
    a, b = browsers
    a.visit("https://a.example/")
    a.storage.setdefault("https://a.example", {})["probe"] = "from-a"
    b.visit("https://b.example/")
    b.storage.setdefault("https://b.example", {})["probe"] = "from-b"
    await first.write_back()
    await second.write_back()
    stored = _storage(profile)
    assert stored["https://a.example"]["probe"] == "from-a"
    assert stored["https://b.example"]["probe"] == "from-b"


async def test_cookies_are_written_back_even_when_reading_storage_runs_out_of_time(
    profile, browsers, monkeypatch
):
    _write(profile, cookies=[_cookie("sid", "old")], origins={"https://slow.example": {"k": "v"}})
    monkeypatch.setattr(sync, "_CAPTURE_DEADLINE_S", 0.3)
    session = ProfileSync(profile, "fake")
    await session.restore()
    chrome = browsers[0]
    chrome.visit("https://fast.example/")
    chrome.visit("https://slow.example/")
    chrome.storage["https://fast.example"] = {"jwt": "new"}
    chrome.storage["https://slow.example"]["k"] = "changed"
    chrome.site_sets_cookie("sid", "logged-in-at-the-end", "shop.example")
    chrome.slow_origins = {"https://slow.example": 5.0}
    await session.write_back()
    assert _jar(profile) == {"sid": "logged-in-at-the-end"}
    stored = _storage(profile)
    assert stored["https://fast.example"] == {"jwt": "new"}
    assert stored["https://slow.example"] == {"k": "v"}


async def test_sites_written_before_the_restore_deadline_count_as_restored(
    profile, browsers, monkeypatch
):
    _write(profile, origins={"https://fast.example": {"k": "v"}, "https://slow.example": {"k": "v"}})
    monkeypatch.setattr(sync, "_RESTORE_DEADLINE_S", 0.3)
    original = FakeChrome.__init__

    def slow(self):
        original(self)
        self.slow_origins = {"https://slow.example": 5.0}

    monkeypatch.setattr(FakeChrome, "__init__", slow)
    session = ProfileSync(profile, "fake")
    restored = await session.restore()
    assert restored.sites == 1 and list(restored.site_failures) == ["https://slow.example"]
    chrome = browsers[0]
    chrome.slow_origins = {}
    chrome.visit("https://fast.example/")
    chrome.storage["https://fast.example"]["k"] = "new-login"
    await session.write_back()
    assert _storage(profile)["https://fast.example"] == {"k": "new-login"}


async def test_a_site_that_expired_before_the_session_stays_expired_when_opened(profile, browsers):
    now = time.time()
    _write(
        profile,
        origins={"https://stale.example": {"old": "month-old"}},
        last_used={"https://stale.example": now - 31 * 86400},
    )
    session = ProfileSync(profile, "fake")
    await session.restore()
    chrome = browsers[0]
    assert "https://stale.example" not in chrome.storage
    chrome.visit("https://stale.example/")
    chrome.storage["https://stale.example"] = {"fresh": "login"}
    await session.checkpoint()
    await session.write_back()
    assert _storage(profile) == {"https://stale.example": {"fresh": "login"}}


async def test_openbrowses_own_pages_are_not_recorded_as_sites(profile, browsers):
    session = ProfileSync(profile, "fake", ignore={"http://127.0.0.1:8420/codeview"})
    await session.restore()
    chrome = browsers[0]
    chrome.visit("http://127.0.0.1:8420/codeview")
    chrome.visit("https://shop.example/")
    await session.write_back()
    meta = json.loads(store.meta_path(profile).read_text())
    assert list(meta["origins"]) == ["https://shop.example"]
