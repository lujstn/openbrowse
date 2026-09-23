"""Three-way storage-state merge — what a session sharing a profile writes back."""

from openbrowse.profiles.merge import merge_storage_states, read_state, write_state


def _cookie(name, value, domain="example.com", path="/"):
    return {"name": name, "value": value, "domain": domain, "path": path}


def _state(*cookies, origins=None):
    return {"cookies": list(cookies), "origins": origins or []}


def _by_name(state):
    return {c["name"]: c["value"] for c in state["cookies"]}


def test_untouched_key_keeps_the_profiles_current_value():
    baseline = _state(_cookie("session", "old"))
    ours = _state(_cookie("session", "old"))
    theirs = _state(_cookie("session", "newer-from-the-other-run"))
    assert _by_name(merge_storage_states(baseline, ours, theirs)) == {
        "session": "newer-from-the-other-run"
    }


def test_our_change_wins_over_the_baseline():
    baseline = _state(_cookie("session", "old"))
    ours = _state(_cookie("session", "ours"))
    theirs = _state(_cookie("session", "old"))
    assert _by_name(merge_storage_states(baseline, ours, theirs)) == {"session": "ours"}


def test_both_sides_add_different_cookies():
    baseline = _state()
    ours = _state(_cookie("a", "1"))
    theirs = _state(_cookie("b", "2"))
    assert _by_name(merge_storage_states(baseline, ours, theirs)) == {"a": "1", "b": "2"}


def test_our_delete_is_applied_when_they_did_not_touch_it():
    baseline = _state(_cookie("a", "1"), _cookie("b", "2"))
    ours = _state(_cookie("a", "1"))
    theirs = _state(_cookie("a", "1"), _cookie("b", "2"))
    assert _by_name(merge_storage_states(baseline, ours, theirs)) == {"a": "1"}


def test_their_newer_write_survives_our_delete():
    baseline = _state(_cookie("session", "old"))
    ours = _state()
    theirs = _state(_cookie("session", "fresh-login"))
    assert _by_name(merge_storage_states(baseline, ours, theirs)) == {
        "session": "fresh-login"
    }


def test_conflicting_writes_take_the_later_one():
    baseline = _state(_cookie("session", "old"))
    ours = _state(_cookie("session", "ours"))
    theirs = _state(_cookie("session", "theirs"))
    assert _by_name(merge_storage_states(baseline, ours, theirs)) == {"session": "ours"}


def test_cookies_on_different_domains_do_not_collide():
    baseline = _state()
    ours = _state(_cookie("id", "a", domain="one.com"))
    theirs = _state(_cookie("id", "b", domain="two.com"))
    merged = merge_storage_states(baseline, ours, theirs)
    assert {(c["domain"], c["value"]) for c in merged["cookies"]} == {
        ("one.com", "a"),
        ("two.com", "b"),
    }


def test_cookies_on_different_paths_do_not_collide():
    baseline = _state()
    ours = _state(_cookie("id", "a", path="/app"))
    theirs = _state(_cookie("id", "b", path="/admin"))
    merged = merge_storage_states(baseline, ours, theirs)
    assert {(c["path"], c["value"]) for c in merged["cookies"]} == {
        ("/app", "a"),
        ("/admin", "b"),
    }


def _origins(origin, kind, pairs):
    return [{"origin": origin, kind: [{"name": k, "value": v} for k, v in pairs.items()]}]


def test_local_storage_merges_per_key():
    baseline = _state(origins=_origins("https://x.test", "localStorage", {"a": "1", "b": "2"}))
    ours = _state(origins=_origins("https://x.test", "localStorage", {"a": "ours", "b": "2"}))
    theirs = _state(origins=_origins("https://x.test", "localStorage", {"a": "1", "b": "theirs"}))
    merged = merge_storage_states(baseline, ours, theirs)
    pairs = {i["name"]: i["value"] for i in merged["origins"][0]["localStorage"]}
    assert pairs == {"a": "ours", "b": "theirs"}


def test_session_storage_merges_alongside_local_storage():
    baseline = _state()
    ours = _state(origins=_origins("https://x.test", "sessionStorage", {"tab": "ours"}))
    theirs = _state(origins=_origins("https://x.test", "localStorage", {"pref": "dark"}))
    merged = merge_storage_states(baseline, ours, theirs)
    entry = merged["origins"][0]
    assert entry["origin"] == "https://x.test"
    assert entry["sessionStorage"] == [{"name": "tab", "value": "ours"}]
    assert entry["localStorage"] == [{"name": "pref", "value": "dark"}]


def test_origins_from_both_sides_are_kept():
    baseline = _state()
    ours = _state(origins=_origins("https://a.test", "localStorage", {"k": "1"}))
    theirs = _state(origins=_origins("https://b.test", "localStorage", {"k": "2"}))
    merged = merge_storage_states(baseline, ours, theirs)
    assert [o["origin"] for o in merged["origins"]] == ["https://a.test", "https://b.test"]


def test_emptied_origin_is_dropped():
    baseline = _state(origins=_origins("https://x.test", "localStorage", {"k": "1"}))
    ours = _state()
    theirs = _state(origins=_origins("https://x.test", "localStorage", {"k": "1"}))
    assert merge_storage_states(baseline, ours, theirs)["origins"] == []


def test_missing_sides_are_treated_as_empty():
    assert merge_storage_states(None, None, None) == {"cookies": [], "origins": []}
    only_ours = merge_storage_states(None, _state(_cookie("a", "1")), None)
    assert _by_name(only_ours) == {"a": "1"}
    only_theirs = merge_storage_states(None, None, _state(_cookie("b", "2")))
    assert _by_name(only_theirs) == {"b": "2"}


def test_merge_output_is_a_normalised_storage_state():
    merged = merge_storage_states(
        None,
        {"cookies": [{"name": "a", "value": "1", "domain": "x.test", "size": 99}]},
        None,
    )
    assert merged["cookies"] == [{"name": "a", "value": "1", "domain": "x.test"}]
    assert merged["origins"] == []


def test_read_state_survives_a_missing_or_corrupt_file(tmp_path):
    assert read_state(tmp_path / "nope.json") is None
    corrupt = tmp_path / "bad.json"
    corrupt.write_text("{not json")
    assert read_state(corrupt) is None
    listy = tmp_path / "list.json"
    listy.write_text("[]")
    assert read_state(listy) is None


def test_write_state_is_atomic_and_round_trips(tmp_path):
    target = tmp_path / "nested" / "state.json"
    state = _state(_cookie("a", "1"))
    write_state(target, state)
    assert read_state(target) == state
    assert not (target.parent / (target.name + ".tmp")).exists()


def test_storage_values_that_are_site_caches_are_not_kept() -> None:
    """browser-use injects every origin's storage into every document; a profile
    that had merged back megabytes of caches wedged page loads outright."""
    from openbrowse.profiles.storage import _MAX_STORAGE_VALUE_CHARS, trim_origins

    origins = [
        {
            "origin": "https://shop.example",
            "localStorage": [
                {"name": "session", "value": "tok-123"},
                {"name": "catalogue_cache", "value": "x" * (_MAX_STORAGE_VALUE_CHARS + 1)},
            ],
        },
        {"origin": "https://cache-only.example", "localStorage": [{"name": "c", "value": "y" * 200_000}]},
    ]
    trimmed = trim_origins(origins)
    assert trimmed == [{"origin": "https://shop.example", "localStorage": [{"name": "session", "value": "tok-123"}]}]


def test_an_origin_over_its_storage_cap_drops_its_largest_values_first() -> None:
    from openbrowse.profiles.storage import _MAX_ORIGIN_STORAGE_CHARS, trim_origins

    big = 60_000
    items = [{"name": f"k{i}", "value": "v" * big} for i in range(6)] + [
        {"name": "login", "value": "short"}
    ]
    trimmed = trim_origins([{"origin": "https://a.example", "localStorage": items}])
    kept = trimmed[0]["localStorage"]
    assert {"name": "login", "value": "short"} in kept
    assert sum(len(i["name"]) + len(i["value"]) for i in kept) <= _MAX_ORIGIN_STORAGE_CHARS


def test_merge_writes_back_a_trimmed_profile() -> None:
    from openbrowse.profiles.merge import merge_storage_states

    bloated = {
        "cookies": [],
        "origins": [{"origin": "https://a.example", "localStorage": [{"name": "cache", "value": "z" * 100_000}]}],
    }
    merged = merge_storage_states(None, bloated, None)
    assert merged["origins"] == []


def test_session_working_copy_is_trimmed_before_the_browser_loads_it(tmp_path, monkeypatch) -> None:
    import asyncio
    import json

    import openbrowse.agent.runner as runner

    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({
        "cookies": [{"name": "sid", "value": "1", "domain": "a.example", "path": "/"}],
        "origins": [{"origin": "https://a.example", "localStorage": [
            {"name": "login", "value": "t"}, {"name": "cache", "value": "q" * 100_000}]}],
    }))
    monkeypatch.setattr(runner, "_session_state_path", lambda sid: tmp_path / f"{sid}.json")
    working, baseline = asyncio.run(runner._open_session_state("s1", profile))
    loaded = json.loads(working.read_text())
    assert loaded["origins"] == [{"origin": "https://a.example", "localStorage": [{"name": "login", "value": "t"}]}]
    assert loaded["cookies"][0]["name"] == "sid"
    assert len(baseline["origins"][0]["localStorage"]) == 2
