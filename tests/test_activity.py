"""Run-lifecycle registry and shared-profile tests."""

from openbrowse.agent import activity


def _reset():
    activity._running_sessions.clear()
    activity._profile_sessions.clear()


def test_active_session_count_tracks_lifecycle():
    _reset()
    assert activity.active_session_count() == 0
    activity.session_started("a")
    activity.session_started("b")
    assert activity.active_session_count() == 2
    activity.session_started("a")
    assert activity.active_session_count() == 2
    activity.session_ended("a")
    assert activity.active_session_count() == 1
    activity.session_ended("missing")
    assert activity.active_session_count() == 1
    activity.session_ended("b")
    assert activity.active_session_count() == 0


def test_profile_is_shared_not_claimed():
    _reset()
    assert activity.join_profile("p1", "s1") == []
    assert activity.join_profile("p1", "s2") == ["s1"]
    assert activity.join_profile("p1", "s3") == ["s1", "s2"]
    assert activity.profile_sessions("p1") == ["s1", "s2", "s3"]


def test_rejoining_does_not_report_self():
    _reset()
    activity.join_profile("p1", "s1")
    assert activity.join_profile("p1", "s1") == []
    assert activity.profile_sessions("p1") == ["s1"]


def test_profiles_are_tracked_independently():
    _reset()
    activity.join_profile("p1", "s1")
    assert activity.join_profile("p2", "s2") == []
    assert activity.profile_sessions("p1") == ["s1"]
    assert activity.profile_sessions("p2") == ["s2"]


def test_leaving_forgets_only_that_session():
    _reset()
    activity.join_profile("p1", "s1")
    activity.join_profile("p1", "s2")
    activity.leave_profile("p1", "s1")
    assert activity.profile_sessions("p1") == ["s2"]
    activity.leave_profile("p1", "s2")
    assert activity.profile_sessions("p1") == []
    assert "p1" not in activity._profile_sessions


def test_leaving_an_unknown_profile_is_harmless():
    _reset()
    activity.leave_profile("nope", "s1")
    activity.join_profile("p1", "s1")
    activity.leave_profile("p1", "other")
    assert activity.profile_sessions("p1") == ["s1"]
