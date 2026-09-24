"""Three-way storage-state merge — what a session sharing a profile writes back."""

from openbrowse.profiles.merge import merge_storage_states


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


def test_session_storage_is_not_carried_between_sessions():
    ours = _state(origins=_origins("https://x.test", "sessionStorage", {"tab": "ours"}))
    theirs = _state(origins=_origins("https://x.test", "localStorage", {"pref": "dark"}))
    merged = merge_storage_states(None, ours, theirs)
    assert merged["origins"] == [
        {"origin": "https://x.test", "localStorage": [{"name": "pref", "value": "dark"}]}
    ]


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


def test_origins_the_session_did_not_read_keep_the_profiles_storage():
    baseline = _state(origins=_origins("https://a.test", "localStorage", {"k": "1"}))
    ours = _state()
    theirs = _state(origins=_origins("https://a.test", "localStorage", {"k": "2"}))
    merged = merge_storage_states(baseline, ours, theirs, origins=set())
    assert merged["origins"] == _origins("https://a.test", "localStorage", {"k": "2"})


def test_an_origin_the_session_read_empty_is_emptied():
    baseline = _state(origins=_origins("https://a.test", "localStorage", {"k": "1"}))
    theirs = _state(origins=_origins("https://a.test", "localStorage", {"k": "1"}))
    merged = merge_storage_states(baseline, _state(), theirs, origins={"https://a.test"})
    assert merged["origins"] == []


def test_a_cookie_read_back_with_chromes_extra_fields_is_unchanged():
    stored = _cookie("sid", "1")
    read_back = {
        **stored,
        "expires": -1,
        "priority": "Medium",
        "sourceScheme": "Secure",
        "sourcePort": 443,
        "size": 4,
        "session": True,
    }
    merged = merge_storage_states(
        _state(stored), _state(read_back), _state(_cookie("sid", "newer"))
    )
    assert _by_name(merged) == {"sid": "newer"}


def test_a_changed_expiry_is_a_change():
    baseline = _state(_cookie("sid", "1") | {"expires": 1000.25})
    ours = _state(_cookie("sid", "1") | {"expires": 5000.0})
    theirs = _state(_cookie("sid", "1") | {"expires": 1000.25})
    merged = merge_storage_states(baseline, ours, theirs)
    assert merged["cookies"][0]["expires"] == 5000.0


def test_partitioned_cookies_do_not_collide_with_unpartitioned_ones():
    plain = _cookie("id", "a")
    partitioned = _cookie("id", "b") | {"partitionKey": {"topLevelSite": "https://top.test"}}
    merged = merge_storage_states(None, _state(plain), _state(partitioned))
    assert sorted(c["value"] for c in merged["cookies"]) == ["a", "b"]
