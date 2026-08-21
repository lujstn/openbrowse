"""The realism lane: real public sites. Assertions here are deliberately tolerant —
third parties redesign, geo-gate and consent-wall at will, so these scenarios prove
the tools run cleanly rather than pinning exact content."""

import pytest

from tests.live.harness import (
    assert_no_doom_loop,
    assert_tool_clean,
    assert_used,
)

pytestmark = pytest.mark.live

HEADLINES_SCHEMA = {
    "type": "object",
    "properties": {
        "headlines": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["headlines"],
}


def test_web_search(run_scenario):
    trace = run_scenario(
        "web_search",
        "Use the search tool with the duckduckgo engine to search for 'BBC News'. "
        "Report the title of the first result shown on the results page.",
    )
    assert_used(trace, "search")
    assert_tool_clean(trace, "search")
    assert_no_doom_loop(trace)


def test_bbc_headlines(run_scenario):
    trace = run_scenario(
        "bbc_headlines",
        "Go to https://www.bbc.co.uk/news and collect the top 5 distinct headlines "
        "currently shown into the output. Dismiss any consent banner first. Each "
        "headline is the visible headline text, verbatim.",
        output_schema=HEADLINES_SCHEMA,
        max_cost_usd=0.25,
        timeout_s=360.0,
    )
    assert_no_doom_loop(trace)
    assert trace.is_successful, trace.describe()
    headlines = (trace.output or {}).get("headlines") or []
    cleaned = {h.strip() for h in headlines if isinstance(h, str) and h.strip()}
    assert len(cleaned) >= 5, f"wanted 5 distinct headlines, got {headlines}.\n{trace.describe()}"
