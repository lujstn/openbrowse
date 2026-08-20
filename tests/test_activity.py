"""Run-lifecycle registry and profile-claim tests."""

from app.agent import activity


def _reset():
    activity._running_sessions.clear()
    activity._claimed_profiles.clear()


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


def test_profile_claim_refuses_second_session():
    _reset()
    assert activity.try_claim_profile("p1", "s1") is None
    assert activity.try_claim_profile("p1", "s2") == "s1"
    assert activity.try_claim_profile("p1", "s1") is None
    assert activity.try_claim_profile("p2", "s2") is None

    activity.release_profile("p1", "s2")
    assert activity.try_claim_profile("p1", "s2") == "s1"

    activity.release_profile("p1", "s1")
    assert activity.try_claim_profile("p1", "s2") is None
