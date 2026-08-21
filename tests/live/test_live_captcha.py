"""The paid captcha solve. Opt-in: the CapSolver key lives on the *server*, so the
harness cannot detect it from its own environment — set LIVE_CAPTCHA=1 only when the
target server is configured with CAPSOLVER_API_KEY."""

import os

import pytest

from tests.live.harness import (
    assert_no_doom_loop,
    assert_tool_clean,
    assert_used,
)

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("LIVE_CAPTCHA") != "1",
        reason="set LIVE_CAPTCHA=1 when the target server has a CapSolver key",
    ),
]


def test_captcha_demo(run_scenario):
    trace = run_scenario(
        "captcha_demo",
        "Go to https://www.google.com/recaptcha/api2/demo. Use the solve_captcha "
        "tool to pass the reCAPTCHA — never click the checkbox or image tiles "
        "yourself. Once solved, click Submit and report the verification message "
        "shown.",
        max_cost_usd=0.15,
        timeout_s=360.0,
    )
    assert_used(trace, "solve_captcha")
    assert_tool_clean(trace, "solve_captcha")
    assert_no_doom_loop(trace)
    # A third-party demo page: the strong claim is that the solver ran cleanly,
    # not the demo site's exact copy.
    assert trace.is_successful, trace.describe()
