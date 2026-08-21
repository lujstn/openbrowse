"""Navigation and page-reading built-ins: extract, go_back, wait, scroll, find_text."""

import pytest

from tests.live.fixture_site import (
    ARTICLE,
    COLOPHON_SENTENCE,
    DELAYED_CODE,
    NAV_CODE,
)
from tests.live.harness import (
    assert_no_doom_loop,
    assert_not_used,
    assert_success,
    assert_used,
)

pytestmark = pytest.mark.live


def _text(trace) -> str:
    return str(trace.output or "")


def test_navigate_extract(run_scenario, fixture_url):
    trace = run_scenario(
        "navigate_extract",
        f"Go to {fixture_url}/article.html. Use the extract tool to pull the "
        "article's title and its author from the page. This is a single-page "
        "extraction: do not use read_pages, find_links or Python scripts. "
        "Report the title and the author name.",
    )
    assert_success(trace)
    assert_used(trace, "extract")
    assert_not_used(trace, "read_pages", "find_links", "run_code_file")
    assert_no_doom_loop(trace)
    assert ARTICLE["title"] in _text(trace), trace.describe()
    assert ARTICLE["author"] in _text(trace), trace.describe()


def test_go_back(run_scenario, fixture_url):
    trace = run_scenario(
        "go_back_nav",
        f"Go to {fixture_url}/nav_a.html. Click through to the store room and note "
        "the store room code. Then return to the front desk using the go_back tool — "
        "do not navigate to the front desk URL again. Confirm you are back on the "
        "Front desk page, then report the store room code exactly.",
    )
    assert_success(trace)
    assert_used(trace, "go_back")
    assert_used(trace, "click")
    assert_no_doom_loop(trace)
    assert NAV_CODE in _text(trace), trace.describe()


def test_wait_delayed(run_scenario, fixture_url):
    trace = run_scenario(
        "wait_delayed",
        f"Go to {fixture_url}/delayed.html. The daily code appears roughly 8 seconds "
        "after the page loads. Use the wait tool to wait for it — do not refresh the "
        "page, and do not read it with JavaScript or scripts. Then report the daily "
        "code exactly.",
    )
    assert_success(trace)
    assert_used(trace, "wait")
    assert_not_used(trace, "evaluate", "run_code_file")
    assert_no_doom_loop(trace)
    assert DELAYED_CODE in _text(trace), trace.describe()


def test_scroll_find(run_scenario, fixture_url):
    trace = run_scenario(
        "scroll_find",
        f"Go to {fixture_url}/long.html. Using scroll and the find_text tool, locate "
        "the section headed 'Colophon' near the bottom of the page and report the "
        "sentence directly beneath that heading, word for word. Do not use "
        "search_page, extract or scripts.",
    )
    assert_success(trace)
    assert trace.used["find_text"] + trace.used["scroll"] >= 1, trace.describe()
    assert_not_used(trace, "search_page", "run_code_file")
    assert_no_doom_loop(trace)
    assert COLOPHON_SENTENCE in _text(trace), trace.describe()
