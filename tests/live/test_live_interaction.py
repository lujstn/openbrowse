"""Element interaction: input, click, send_keys, dropdowns, evaluate, find_elements,
search_page — including the direct regression scenario for the find_elements loop."""

import pytest

from tests.live.fixture_site import (
    ARTICLE,
    DROPDOWN_TARGET,
    SEARCH_PAGE_SENTENCE,
    SOCIAL_LINKS,
)
from tests.live.harness import (
    assert_no_doom_loop,
    assert_not_used,
    assert_success,
    assert_tool_clean,
    assert_used,
)

pytestmark = pytest.mark.live


def _text(trace) -> str:
    return str(trace.output or "")


def test_form_typing(run_scenario, fixture_url):
    trace = run_scenario(
        "form_typing",
        f"Go to {fixture_url}/form.html. Type 'wardian case' into the catalogue "
        "search box using the input tool, click the Search button, and report the "
        "result line shown, exactly.",
    )
    assert_success(trace)
    assert_used(trace, "input")
    assert_used(trace, "click")
    assert_no_doom_loop(trace)
    assert "You searched for: wardian case" in _text(trace), trace.describe()


def test_enter_submit(run_scenario, fixture_url):
    trace = run_scenario(
        "enter_submit",
        f"Go to {fixture_url}/enter_form.html. This form has no submit button. Type "
        "'fernery' into the box, then submit it by pressing Enter using the "
        "send_keys tool. Report the result line shown, exactly.",
    )
    assert_success(trace)
    assert_used(trace, "send_keys")
    assert_no_doom_loop(trace)
    assert "You searched for: fernery" in _text(trace), trace.describe()


def test_dropdown(run_scenario, fixture_url):
    trace = run_scenario(
        "dropdown",
        f"Go to {fixture_url}/dropdown.html. Inspect the 'Vegetable of the month' "
        "select with the dropdown_options tool, then choose "
        f"{DROPDOWN_TARGET} with the select_dropdown tool. Do not set the value "
        "with JavaScript. Report the confirmation line shown, exactly.",
    )
    assert_success(trace)
    assert_used(trace, "select_dropdown")
    assert_not_used(trace, "evaluate")
    assert_no_doom_loop(trace)
    assert f"You chose: {DROPDOWN_TARGET}" in _text(trace), trace.describe()


def test_evaluate_js(run_scenario, fixture_url):
    trace = run_scenario(
        "evaluate_js",
        f"Go to {fixture_url}/article.html. Use the evaluate tool to run JavaScript "
        "that reads window.__secret, and report its value exactly.",
    )
    assert_success(trace)
    assert_used(trace, "evaluate")
    assert_no_doom_loop(trace)
    assert ARTICLE["secret"] in _text(trace), trace.describe()


@pytest.mark.sonnet_smoke
def test_find_elements_social(run_scenario, fixture_url):
    """Regression for session 19798367: nine identical find_elements calls because
    the output guard hid the repeated result. The loop assertion fails this scenario
    even when the run eventually 'succeeds'."""
    trace = run_scenario(
        "find_elements_social",
        f"Go to {fixture_url}/social.html. Using the find_elements tool with its "
        "attributes parameter so hrefs are returned, collect the complete list of "
        f"social profile URLs on the page — there are {len(SOCIAL_LINKS)}. Report "
        "every URL.",
    )
    assert_success(trace)
    assert_used(trace, "find_elements")
    assert_tool_clean(trace, "find_elements")
    assert_no_doom_loop(trace)
    missing = [url for url in SOCIAL_LINKS if url not in _text(trace)]
    assert not missing, f"missing {len(missing)} URLs, e.g. {missing[:3]}.\n{trace.describe()}"


def test_search_page(run_scenario, fixture_url):
    trace = run_scenario(
        "search_page",
        f"Go to {fixture_url}/long.html. Use the search_page tool to find the word "
        "'zeppelin' on the page, and report the full sentence containing it, "
        "exactly. Do not use extract or scripts.",
    )
    assert_success(trace)
    assert_used(trace, "search_page")
    assert_not_used(trace, "extract", "run_code_file")
    assert_no_doom_loop(trace)
    assert SEARCH_PAGE_SENTENCE in _text(trace), trace.describe()
