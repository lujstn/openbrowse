"""Tab management, both families: browser-use's switch/close by tab id, and
OpenBrowse's queued open_tabs/goto_tab/close_tab plus open_in_new_tab."""

import pytest

from tests.live.fixture_site import ARTICLE, STAFF
from tests.live.harness import (
    assert_no_doom_loop,
    assert_not_used,
    assert_success,
    assert_used,
)

pytestmark = pytest.mark.live


def _text(trace) -> str:
    return str(trace.output or "")


def test_builtin_tabs(run_scenario, fixture_url):
    trace = run_scenario(
        "builtin_tabs",
        f"Go to {fixture_url}/tabs.html. Click the 'Read the featured article' link — "
        "it opens in a new tab. Then use the switch tool to return to the Reading "
        "room tab, and the close tool to close the article tab. Do not use "
        "open_tabs, goto_tab, open_in_new_tab or close_tab. Report the titles of "
        "both pages.",
    )
    assert_success(trace)
    assert_used(trace, "switch")
    assert_used(trace, "close")
    assert_not_used(trace, "open_tabs", "goto_tab", "open_in_new_tab", "close_tab")
    assert_no_doom_loop(trace)
    assert ARTICLE["title"] in _text(trace), trace.describe()
    assert "Reading room" in _text(trace), trace.describe()


def test_ob_tab_family(run_scenario, fixture_url):
    urls = ", ".join(f"{fixture_url}/detail/{n}.html" for n in range(1, 7))
    trace = run_scenario(
        "ob_tab_family",
        f"Queue these six pages as background tabs using the open_tabs tool: {urls}. "
        "Then use goto_tab to load tab index 1 and tab index 3 (0-based, in that "
        "order), and after reading each page close it with close_tab. Report the "
        "Name line from each of the two pages you visited.",
    )
    assert_success(trace)
    assert_used(trace, "open_tabs")
    assert_used(trace, "goto_tab", at_least=2)
    assert_used(trace, "close_tab")
    assert_no_doom_loop(trace)
    assert STAFF[1]["name"] in _text(trace), trace.describe()
    assert STAFF[3]["name"] in _text(trace), trace.describe()


def test_open_in_new_tab(run_scenario, fixture_url):
    trace = run_scenario(
        "open_in_new_tab",
        f"Go to {fixture_url}/listing.html. Use the open_in_new_tab tool to open the "
        f"link for the third staff member, {STAFF[2]['name']}, in a new tab, and "
        "report the Role line from that profile page.",
    )
    assert_success(trace)
    assert_used(trace, "open_in_new_tab")
    assert_no_doom_loop(trace)
    assert STAFF[2]["role"] in _text(trace), trace.describe()
