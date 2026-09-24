"""A profile on disk: the storage_state file, its sidecar, and migration."""

from __future__ import annotations

import json

from openbrowse.profiles import policy, store


def _state(cookies=(), origins=None):
    return {
        "cookies": list(cookies),
        "origins": [
            {"origin": o, "localStorage": [{"name": k, "value": v} for k, v in items.items()]}
            for o, items in (origins or {}).items()
        ],
    }


def _cookie(name, domain):
    return {"name": name, "value": "1", "domain": domain, "path": "/"}


def test_save_and_load_round_trip_with_the_sidecar(tmp_path):
    path = tmp_path / "p.json"
    state = _state([_cookie("sid", ".a.example")], {"https://a.example": {"k": "v"}})
    store.save(path, store.ProfileData(state, {"https://a.example": 1_000.0}), now=2_000.0)

    assert json.loads(path.read_text()) == state
    meta = json.loads(store.meta_path(path).read_text())
    assert meta == {"version": 1, "origins": {"https://a.example": {"lastUsed": 1_000.0}}}
    loaded = store.load(path)
    assert loaded.state == state and loaded.last_used == {"https://a.example": 1_000.0}
    assert not list(tmp_path.glob("*.tmp"))


def test_the_profile_file_stays_a_plain_storage_state(tmp_path):
    path = tmp_path / "p.json"
    store.save(path, store.ProfileData(_state(origins={"https://a.example": {"k": "v"}})))
    assert set(json.loads(path.read_text())) == {"cookies", "origins"}


def test_load_survives_a_missing_corrupt_or_sidecarless_profile(tmp_path):
    assert store.load(tmp_path / "missing.json") is None
    (tmp_path / "bad.json").write_text("{not json")
    assert store.load(tmp_path / "bad.json") is None
    (tmp_path / "list.json").write_text("[]")
    assert store.load(tmp_path / "list.json") is None
    (tmp_path / "plain.json").write_text(json.dumps(_state()))
    assert store.load(tmp_path / "plain.json").last_used == {}
    store.meta_path(tmp_path / "plain.json").write_text("garbage")
    assert store.load(tmp_path / "plain.json").last_used == {}


def test_save_applies_the_limits(tmp_path):
    path = tmp_path / "p.json"
    origins = {f"https://s{i}.example": {"k": "v"} for i in range(policy.MAX_SITES + 3)}
    applied = store.save(path, store.ProfileData(_state(origins=origins)))
    assert len(json.loads(path.read_text())["origins"]) == policy.MAX_SITES
    assert len(applied.evicted_origins) == 3


def test_move_and_remove_take_the_sidecar_with_them(tmp_path):
    old, new = tmp_path / "old.json", tmp_path / "sub" / "new.json"
    store.save(old, store.ProfileData())
    store.move(old, new)
    assert new.exists() and store.meta_path(new).exists()
    assert not old.exists() and not store.meta_path(old).exists()
    store.remove(new)
    assert not new.exists() and not store.meta_path(new).exists()


def test_sites_group_cookie_domains_and_origins_like_browser_settings():
    data = store.ProfileData(
        _state(
            [_cookie("a", ".shop.example"), _cookie("b", "www.shop.example"), _cookie("c", ".bank.example")],
            {"https://www.shop.example": {"cart": "12345"}, "https://app.shop.example": {"x": "1"}},
        ),
        {"https://app.shop.example": 500.0},
    )
    sites = {s.name: s for s in store.sites(data)}
    assert set(sites) == {"shop.example", "bank.example"}
    assert sites["shop.example"].cookies == 2
    assert sites["shop.example"].storage_chars == len("cart12345") + len("x1")
    assert sites["shop.example"].last_used == 500.0
    assert sites["bank.example"].storage_chars == 0


def test_without_site_removes_its_cookies_storage_and_history():
    data = store.ProfileData(
        _state(
            [_cookie("a", ".shop.example"), _cookie("c", ".bank.example")],
            {"https://app.shop.example": {"x": "1"}, "https://bank.example": {"y": "2"}},
        ),
        {"https://app.shop.example": 1.0, "https://bank.example": 2.0},
    )
    left = store.without_site(data, "shop.example")
    assert [c["domain"] for c in left.state["cookies"]] == [".bank.example"]
    assert [e["origin"] for e in left.state["origins"]] == ["https://bank.example"]
    assert left.last_used == {"https://bank.example": 2.0}


def test_migration_backs_up_limits_and_runs_once(tmp_path):
    bloated = _state(
        [_cookie("sid", ".keep.example")],
        {f"https://s{i}.example": {"cache": "x" * 60_000} for i in range(80)}
        | {"https://keep.example": {"token": "t"}},
    )
    bloated["origins"][0]["sessionStorage"] = [{"name": "tab", "value": "1"}]
    path = tmp_path / "p1.json"
    original = json.dumps(bloated)
    path.write_text(original)
    (tmp_path / "p1.json.import-bak").write_text("not a profile")

    assert store.migrate(tmp_path) == 1
    assert (tmp_path / "p1.json.pre-limits").read_text() == original
    migrated = json.loads(path.read_text())
    assert len(migrated["origins"]) <= policy.MAX_SITES
    assert policy.storage_chars(migrated) <= policy.MAX_STORAGE_CHARS
    assert "https://keep.example" in [e["origin"] for e in migrated["origins"]]
    assert all("sessionStorage" not in e for e in migrated["origins"])

    assert store.migrate(tmp_path) == 0
    assert json.loads(path.read_text()) == migrated


def test_migration_leaves_a_file_that_is_not_a_storage_state(tmp_path):
    (tmp_path / "odd.json").write_text(json.dumps({"cookies": "nope"}))
    assert store.migrate(tmp_path) == 0
    assert json.loads((tmp_path / "odd.json").read_text()) == {"cookies": "nope"}
