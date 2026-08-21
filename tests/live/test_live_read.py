"""The flagship bulk-read pipeline: find_links → read_pages (with cross-origin
iframes) → schema store, verified against exact ground truth."""

import pytest

from tests.live.fixture_site import STAFF
from tests.live.harness import (
    assert_no_doom_loop,
    assert_output,
    assert_success,
    assert_used,
)

pytestmark = pytest.mark.live

STAFF_SCHEMA = {
    "type": "object",
    "properties": {
        "staff": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "role": {"type": "string"},
                    "dailyRateGbp": {"type": "integer"},
                    "city": {"type": "string"},
                },
                "required": ["name", "role", "dailyRateGbp", "city"],
            },
        }
    },
    "required": ["staff"],
}


@pytest.mark.sonnet_smoke
def test_listing_pipeline(run_scenario, fixture_url):
    trace = run_scenario(
        "listing_pipeline",
        f"Go to {fixture_url}/listing.html — a staff list with 12 profile links. "
        "Collect the profile links with the find_links tool, then read every "
        "profile in one read_pages sweep (two of the profiles embed their facts in "
        "an iframe; read_pages handles frames). Load all 12 rows into the output "
        "with every field filled: name, role, dailyRateGbp, city.",
        output_schema=STAFF_SCHEMA,
        max_cost_usd=0.25,
        timeout_s=420.0,
    )
    assert_success(trace)
    assert_used(trace, "find_links")
    assert_used(trace, "read_pages")
    assert_no_doom_loop(trace)
    assert_output(trace, {"staff": STAFF})
