"""Tests for the gentle primaryUrl reminder (app/agent/reminder.py)."""

from app.agent.reminder import PrimaryUrlReminder, links_opened, norm_url

START = "https://a.com/"
JOBS = "https://a.com/jobs"
OTHER = "https://a.com/other"


class _FakeAction:
    def __init__(self, dump: dict) -> None:
        self._dump = dump

    def model_dump(self, exclude_none: bool = True) -> dict:
        return self._dump


def test_norm_url():
    assert norm_url("https://a.com/jobs/") == JOBS
    assert norm_url("https://a.com/jobs#frag") == JOBS
    assert norm_url("https://a.com/jobs?q=1") == "https://a.com/jobs?q=1"
    assert norm_url(None) is None
    assert norm_url("") is None


def test_links_opened():
    assert links_opened([_FakeAction({"open_tabs": {"urls": ["a", "b", "c"]}})]) == 3
    assert links_opened([_FakeAction({"navigate": {"url": "x"}})]) == 0
    assert links_opened(None) == 0
    assert links_opened([_FakeAction({"open_tabs": {"urls": []}})]) == 0


def test_unset_nudges_once_then_never():
    r = PrimaryUrlReminder()
    nudges = []
    for step in range(1, 9):
        cur = START if step == 1 else JOBS
        nudges.append(r.observe(step, cur, 0, None, START))
    assert nudges[:4] == [None, None, None, None]
    fired = [i for i, n in enumerate(nudges) if n]
    assert len(fired) == 1
    assert "primaryUrl" in nudges[fired[0]]
    assert JOBS in nudges[fired[0]]


def test_starturl_never_triggers():
    r = PrimaryUrlReminder()
    nudges = [r.observe(step, START, 0, None, START) for step in range(1, 12)]
    assert all(n is None for n in nudges)


def test_fanout_triggers_without_repeat_visits():
    r = PrimaryUrlReminder()
    out = []
    for step in range(1, 6):
        if step == 5:
            out.append(r.observe(step, OTHER, 5, None, START))
        else:
            out.append(r.observe(step, START, 0, None, START))
    assert out[-1] is not None
    assert OTHER in out[-1]


def test_pin_then_drift_nudges_once():
    r = PrimaryUrlReminder()
    n1 = r.observe(1, JOBS, 0, JOBS, START)
    assert n1 is None
    results = []
    for step in range(2, 9):
        results.append(r.observe(step, OTHER, 0, JOBS, START))
    fired = [i for i, n in enumerate(results) if n]
    assert len(fired) == 1
    msg = results[fired[0]]
    assert JOBS in msg and OTHER in msg


def test_pin_and_stay_put_never_nudges():
    r = PrimaryUrlReminder()
    r.observe(1, JOBS, 0, JOBS, START)
    nudges = [r.observe(step, JOBS, 0, JOBS, START) for step in range(2, 12)]
    assert all(n is None for n in nudges)


def test_setting_primary_after_unset_nudge_resets_to_stale_tracking():
    r = PrimaryUrlReminder()
    for step in range(1, 6):
        cur = START if step == 1 else JOBS
        r.observe(step, cur, 0, None, START)
    assert r.unset_reminded is True
    later = [r.observe(step, JOBS, 0, JOBS, START) for step in range(6, 14)]
    assert all(n is None for n in later)
