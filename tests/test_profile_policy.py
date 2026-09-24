"""What a profile keeps: browser-style limits on cookies and each site's stored data."""

from __future__ import annotations

from openbrowse.profiles import policy

NOW = 1_800_000_000.0
DAY = 86400.0


def _origin(name, chars=10, key="k"):
    return {"origin": f"https://{name}", "localStorage": [{"name": key, "value": "v" * chars}]}


def _state(cookies=(), origins=()):
    return {"cookies": list(cookies), "origins": list(origins)}


def _cookie(name, domain="a.example", **extra):
    return {"name": name, "value": "1", "domain": domain, "path": "/", **extra}


def _origins(applied):
    return [e["origin"] for e in applied.state["origins"]]


def test_session_storage_is_dropped_and_local_storage_kept():
    state = _state(origins=[{
        "origin": "https://a.example",
        "localStorage": [{"name": "keep", "value": "1"}],
        "sessionStorage": [{"name": "tab", "value": "2"}],
    }])
    applied = policy.apply(state, {}, now=NOW)
    assert applied.state["origins"] == [
        {"origin": "https://a.example", "localStorage": [{"name": "keep", "value": "1"}]}
    ]


def test_a_site_is_kept_or_evicted_whole_never_partly():
    login_plus_cache = {
        "origin": "https://shop.example",
        "localStorage": [
            {"name": "token", "value": "t"},
            {"name": "cache", "value": "c" * policy.MAX_SITE_CHARS},
        ],
    }
    applied = policy.apply(_state(origins=[login_plus_cache, _origin("ok.example")]), {}, now=NOW)
    assert _origins(applied) == ["https://ok.example"]
    assert applied.evicted_origins == ["https://shop.example"]


def test_least_recently_used_sites_go_first_past_the_site_limit():
    origins = [_origin(f"s{i}.example") for i in range(policy.MAX_SITES + 5)]
    last_used = {o["origin"]: NOW - i * 60 for i, o in enumerate(origins)}
    applied = policy.apply(_state(origins=origins), last_used, now=NOW)
    assert len(applied.state["origins"]) == policy.MAX_SITES
    assert set(applied.evicted_origins) == {o["origin"] for o in origins[policy.MAX_SITES:]}


def test_least_recently_used_sites_go_first_past_the_size_budget():
    third = policy.MAX_STORAGE_CHARS // 3 - 10
    origins = [_origin(f"s{i}.example", chars=third) for i in range(4)]
    last_used = {o["origin"]: NOW - i * 60 for i, o in enumerate(origins)}
    applied = policy.apply(_state(origins=origins), last_used, now=NOW)
    assert _origins(applied) == ["https://s0.example", "https://s1.example", "https://s2.example"]
    assert policy.storage_chars(applied.state) <= policy.MAX_STORAGE_CHARS


def test_an_older_small_site_still_fits_after_a_newer_large_one_does_not():
    big = _origin("big.example", chars=policy.MAX_SITE_CHARS - 10)
    bigger = _origin("bigger.example", chars=policy.MAX_SITE_CHARS - 10)
    small = _origin("small.example")
    last_used = {
        "https://big.example": NOW,
        "https://bigger.example": NOW - 60,
        "https://small.example": NOW - 120,
    }
    applied = policy.apply(
        _state(origins=[big, bigger, _origin("filler.example", chars=policy.MAX_SITE_CHARS // 2), small]),
        last_used | {"https://filler.example": NOW - 30},
        now=NOW,
    )
    assert "https://small.example" in _origins(applied)
    assert "https://bigger.example" in applied.evicted_origins


def test_a_site_not_visited_for_thirty_days_loses_its_data():
    last_used = {"https://idle.example": NOW - 31 * DAY, "https://recent.example": NOW - 29 * DAY}
    applied = policy.apply(
        _state(origins=[_origin("idle.example"), _origin("recent.example")]), last_used, now=NOW
    )
    assert _origins(applied) == ["https://recent.example"]
    assert "https://idle.example" not in applied.last_used


def test_a_site_no_session_has_opened_is_recorded_as_added_not_used():
    applied = policy.apply(_state(origins=[_origin("new.example")]), {}, now=NOW)
    assert applied.last_used == {}
    assert applied.added == {"https://new.example": NOW}


def test_a_site_added_long_ago_and_never_opened_expires():
    applied = policy.apply(
        _state(origins=[_origin("stale.example")]), {}, now=NOW,
        added={"https://stale.example": NOW - 31 * DAY},
    )
    assert _origins(applied) == []


def test_sites_added_at_the_same_moment_as_a_use_still_rank_below_it():
    origins = [_origin(f"s{i}.example") for i in range(policy.MAX_SITES)]
    origins.append(_origin("used.example", chars=50_000))
    added = {o["origin"]: NOW for o in origins if o["origin"] != "https://used.example"}
    first = policy.apply(_state(origins=origins), {"https://used.example": NOW - 60}, now=NOW, added=added)
    assert "https://used.example" in _origins(first)


def test_among_equally_recent_sites_signed_in_and_smaller_ones_are_kept():
    origins = [_origin(f"s{i}.example", chars=100 + i) for i in range(policy.MAX_SITES)]
    origins.append(_origin("login.example", chars=5000))
    cookies = [_cookie("sid", ".login.example")]
    applied = policy.apply(_state(cookies, origins), {}, now=NOW)
    assert "https://login.example" in _origins(applied)
    assert applied.evicted_origins == [f"https://s{policy.MAX_SITES - 1}.example"]


def test_expired_cookies_go_and_session_cookies_stay():
    cookies = [
        _cookie("expired", expires=NOW - 1),
        _cookie("future", expires=NOW + DAY),
        _cookie("session", expires=-1),
        _cookie("no-expiry"),
    ]
    applied = policy.apply(_state(cookies), {}, now=NOW)
    assert sorted(c["name"] for c in applied.state["cookies"]) == ["future", "no-expiry", "session"]
    assert applied.dropped_cookies == 1


def test_a_cookie_chrome_would_refuse_for_size_is_dropped():
    applied = policy.apply(
        _state([{"name": "huge", "value": "x" * policy.MAX_COOKIE_CHARS, "domain": "a.example"}]),
        {},
        now=NOW,
    )
    assert applied.state["cookies"] == []


def test_past_the_jar_limit_cookies_for_sites_used_longest_ago_go_first():
    cookies = [_cookie(f"old{i}", "old.example") for i in range(policy.MAX_COOKIES)]
    cookies += [_cookie(f"new{i}", "new.example") for i in range(10)]
    last_used = {"https://old.example": NOW - DAY, "https://new.example": NOW}
    applied = policy.apply(_state(cookies), last_used, now=NOW)
    names = {c["name"] for c in applied.state["cookies"]}
    assert len(names) == policy.MAX_COOKIES
    assert {f"new{i}" for i in range(10)} <= names


def test_duplicate_cookies_collapse_to_the_last_one():
    applied = policy.apply(
        _state([_cookie("sid") | {"value": "1"}, _cookie("sid") | {"value": "2"}]), {}, now=NOW
    )
    assert [c["value"] for c in applied.state["cookies"]] == ["2"]


def test_origins_are_spelled_as_location_origin_spells_them():
    state = _state(origins=[
        {"origin": "HTTPS://Shop.Example:443/", "localStorage": [{"name": "a", "value": "1"}]},
        {"origin": "https://shop.example", "localStorage": [{"name": "b", "value": "2"}]},
        {"origin": "chrome-extension://abc", "localStorage": [{"name": "c", "value": "3"}]},
        {"origin": "http://localhost:8080", "localStorage": [{"name": "d", "value": "4"}]},
    ])
    applied = policy.apply(state, {}, now=NOW)
    assert applied.state["origins"] == [
        {"origin": "http://localhost:8080", "localStorage": [{"name": "d", "value": "4"}]},
        {
            "origin": "https://shop.example",
            "localStorage": [{"name": "a", "value": "1"}, {"name": "b", "value": "2"}],
        },
    ]


def test_applying_twice_changes_nothing():
    origins = [_origin(f"s{i}.example", chars=40_000) for i in range(80)]
    first = policy.apply(_state([_cookie("sid")], origins), {}, now=NOW)
    second = policy.apply(first.state, first.last_used, now=NOW, added=first.added)
    assert second.state == first.state
    assert second.evicted_origins == []


def test_a_site_with_a_record_of_use_outranks_ones_without():
    origins = [_origin(f"s{i}.example") for i in range(policy.MAX_SITES)]
    origins.append(_origin("used.example", chars=50_000))
    applied = policy.apply(_state(origins=origins), {"https://used.example": NOW}, now=NOW)
    assert "https://used.example" in _origins(applied)
    assert len(applied.evicted_origins) == 1
