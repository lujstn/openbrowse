"""Server-side networking, the Python sandbox, and the session clipboard."""

import pytest

from tests.live.fixture_site import DATA_JSON, NAV_CODE, NUMBERS_SUM
from tests.live.harness import (
    assert_no_doom_loop,
    assert_success,
    assert_used,
    count_calls,
)

pytestmark = pytest.mark.live


def _text(trace) -> str:
    return str(trace.output or "")


def test_http_fetch(run_scenario, fixture_url):
    trace = run_scenario(
        "http_fetch",
        f"Fetch {fixture_url}/api/data.json with the http_fetch tool — it is a JSON "
        "API, so do not read it by opening it in a browser tab. Report the value of "
        "its 'launchCode' field exactly.",
    )
    assert_success(trace)
    assert_used(trace, "http_fetch")
    assert_no_doom_loop(trace)
    assert DATA_JSON["launchCode"] in _text(trace), trace.describe()


def test_run_code(run_scenario, fixture_url):
    trace = run_scenario(
        "run_code",
        f"Go to {fixture_url}/numbers.html — a list of 50 seed counts. Use "
        "run_code_file to compute their exact sum with Python (read the numbers "
        "from the page inside the script, e.g. via browser.get_html or the saved "
        "pages data — do not add them up yourself). Report the total exactly.",
    )
    assert_success(trace)
    assert_used(trace, "run_code_file")
    assert_no_doom_loop(trace)
    assert str(NUMBERS_SUM) in _text(trace), trace.describe()


def test_remember_recall(run_scenario, fixture_url):
    trace = run_scenario(
        "remember_recall",
        f"Step 1: go to {fixture_url}/nav_b.html and store the store room code with "
        "the remember tool under the key 'storecode'. Step 2: go to "
        f"{fixture_url}/form.html. Step 3: fetch the code back with the recall tool "
        "(key 'storecode'), type the recalled code into the search box, submit, and "
        "report the result line shown, exactly.",
    )
    assert_success(trace)
    assert_used(trace, "remember")
    # Every session recalls startUrl at step 1, so only an argument-level check
    # proves the custom key was recalled.
    assert count_calls(trace, "recall", {"key": "storecode"}) >= 1, trace.describe()
    assert_no_doom_loop(trace)
    assert f"You searched for: {NAV_CODE}" in _text(trace), trace.describe()
