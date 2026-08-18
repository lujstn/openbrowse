"""Tests for the captcha subsystem: strategies, dispatch, pipeline, hygiene."""

from __future__ import annotations

import pathlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.agent.captcha import all_strategies, strategy_for
from app.agent.captcha.base import Detection, SolveContext, TokenStrategy
from app.agent.captcha.registry import detect_from_probe
from app.agent.captcha import cdp as cdp_mod, pipeline, probe as probe_mod


# @nonobvious(mirrors): the solving service's published task list. A task type
# absent here is one the service will refuse, so it must never be sent.
SERVICE_TASK_TYPES = {
    "AwsWafClassification", "ImageToTextTask", "ReCaptchaV2Classification",
    "VisionEngine", "AntiTurnstileTaskProxyLess", "ReCaptchaV3TaskProxyLess",
    "ReCaptchaV3EnterpriseTaskProxyLess", "ReCaptchaV3EnterpriseTask",
    "ReCaptchaV3Task", "ReCaptchaV2TaskProxyLess",
    "ReCaptchaV2EnterpriseTaskProxyLess", "AntiAwsWafTask",
    "AntiAwsWafTaskProxyLess", "ReCaptchaV2EnterpriseTask",
    "MtCaptchaTaskProxyLess", "MtCaptchaTask", "GeeTestTaskProxyLess",
    "AntiCloudflareTask",
}

SAMPLE_PROBES = {
    "recaptcha_v2": {"kind": "recaptcha_v2", "siteKey": "sk", "dataS": "ds",
                     "apiOrigin": "https://widget.example", "confidence": 20},
    "recaptcha_v2_enterprise": {"kind": "recaptcha_v2_enterprise", "siteKey": "sk",
                                "dataS": "ds", "apiOrigin": "https://widget.example",
                                "confidence": 20},
    "recaptcha_v3": {"kind": "recaptcha_v3", "siteKey": "sk",
                     "apiOrigin": "https://widget.example", "confidence": 12},
    "recaptcha_v3_enterprise": {"kind": "recaptcha_v3_enterprise", "siteKey": "sk",
                                "apiOrigin": "https://widget.example", "confidence": 12},
    "turnstile": {"kind": "turnstile", "siteKey": "sk", "confidence": 20},
    "geetest_v3": {"kind": "geetest_v3", "gt": "g", "challenge": "c", "confidence": 15},
    "geetest_v4": {"kind": "geetest_v4", "captchaId": "cid", "confidence": 15},
    "mtcaptcha": {"kind": "mtcaptcha", "siteKey": "sk", "confidence": 15},
    "awswaf_token": {"kind": "awswaf_token", "confidence": 12},
}

# @nonobvious(mirrors): strategies the page probe cannot name, reached only by an
# explicit hint on solve_captcha, with the params that hint can actually supply.
HINT_ONLY = {
    "imagetotext": {"answer_selector": "#answer", "image_b64": "aW1n"},
}


def _ctx(**kw):
    async def emit(_):
        return None
    base = dict(
        session=SimpleNamespace(),
        page_url="https://site.example/path",
        host="site.example",
        cookies="a=1;b=2",
        emit=emit,
        cost_sink=[],
    )
    base.update(kw)
    return SolveContext(**base)


def test_solve_captcha_action_registers():
    from browser_use import Tools
    from app.agent.captcha.tools import register_captcha_tools
    with patch("app.agent.captcha.tools.settings") as s:
        s.capsolver_api_key = "k"
        tools = Tools()
        register_captcha_tools(tools, [], None)
        assert "solve_captcha" in tools.registry.registry.actions


def test_solve_captcha_absent_without_key():
    from browser_use import Tools
    from app.agent.captcha.tools import register_captcha_tools
    with patch("app.agent.captcha.tools.settings") as s:
        s.capsolver_api_key = ""
        tools = Tools()
        register_captcha_tools(tools, [], None)
        assert "solve_captcha" not in tools.registry.registry.actions


def test_all_strategies_registered_and_unique():
    strats = all_strategies()
    kinds = [s.kind for s in strats]
    assert len(kinds) == len(set(kinds))
    assert len(kinds) >= 14


def test_no_task_type_is_sent_that_the_service_does_not_offer():
    for kind, probe in SAMPLE_PROBES.items():
        strat = strategy_for(kind)
        det = strat.detect(probe)
        task = strat.build_task(det, _ctx())
        assert task["type"] in SERVICE_TASK_TYPES, (
            f"{kind} sends {task['type']!r}, which the service does not offer"
        )


def test_challenges_the_service_cannot_solve_are_named_not_charged():
    for kind in ("hcaptcha", "datadome"):
        strat = strategy_for(kind)
        assert strat is not None, f"{kind} must still be recognised"
        assert strat.unsupported_reason, f"{kind} must say why it cannot be solved"


async def test_an_unsupported_challenge_spends_nothing():
    strat = strategy_for("hcaptcha")
    det = Detection(kind="hcaptcha")
    ctx = _ctx()
    res = await pipeline.run_solve(strat, det, ctx, {})
    assert res.error and "hcaptcha" in res.error
    assert ctx.cost_sink == []


def test_every_strategy_detects_nothing_on_empty_probe():
    for s in all_strategies():
        assert s.detect({}) is None, s.kind


@pytest.mark.parametrize("kind", list(SAMPLE_PROBES))
def test_sample_probe_detects_and_builds(kind):
    strat = strategy_for(kind)
    assert strat is not None
    det = strat.detect(SAMPLE_PROBES[kind])
    assert det is not None and det.kind == kind
    task = strat.build_task(det, _ctx())
    assert isinstance(task.get("type"), str) and task["type"]
    assert strat.solution_keys


def test_recaptcha_v2_build_carries_generic_fields():
    strat = strategy_for("recaptcha_v2")
    det = strat.detect({"kind": "recaptcha_v2", "siteKey": "SK", "dataS": "DS",
                        "invisible": True, "apiOrigin": "https://api.host",
                        "interstitial": True, "confidence": 20})
    task = strat.build_task(det, _ctx(cookies="c=1"))
    assert task["type"] == "ReCaptchaV2TaskProxyLess"
    assert task["websiteKey"] == "SK"
    assert task["recaptchaDataSValue"] == "DS"
    assert task["isInvisible"] is True
    assert task["apiDomain"] == "https://api.host/"
    assert task["cookies"] == "c=1"


def test_recaptcha_v2_apidomain_derived_not_hardcoded():
    strat = strategy_for("recaptcha_v2")
    det = strat.detect({"kind": "recaptcha_v2", "siteKey": "SK",
                        "apiOrigin": "https://other-provider.test", "confidence": 20})
    task = strat.build_task(det, _ctx())
    assert task["apiDomain"] == "https://other-provider.test/"


def test_image_grid_uses_the_token_path_not_a_click_path():
    probe = {"kind": "recaptcha_v2", "siteKey": "sk", "question": "select buses",
             "confidence": 20}
    det = detect_from_probe(probe)
    assert det.kind == "recaptcha_v2"


def test_geetest_v4_declares_all_fields():
    strat = strategy_for("geetest_v4")
    assert set(strat.solution_keys) == {
        "captcha_id", "captcha_output", "gen_time", "lot_number", "pass_token"
    }


class _FakeStrategy(TokenStrategy):
    kind = "fake"
    solution_keys = ("token",)

    def __init__(self, interstitial=False):
        self._interstitial = interstitial
        self.placed = []

    def detect(self, probe):
        return None

    def build_task(self, det, ctx):
        return {"type": "FakeTask", "websiteURL": ctx.page_url, "websiteKey": "k"}

    async def _place(self, session, solution, det):
        self.placed.append(solution)


def _det(interstitial=False):
    return Detection(kind="fake", params={}, interstitial=interstitial)


def _mock_capsolver(create=None, results=None):
    create = create or {"taskId": "T1"}
    results = results or [{"status": "ready", "solution": {"token": "tok"}, "cost": "0.001"}]
    it = iter(results)
    return (
        AsyncMock(return_value=create),
        AsyncMock(side_effect=lambda *a, **k: next(it)),
    )


async def test_pipeline_reports_cleared_when_page_advances():
    strat = _FakeStrategy(interstitial=True)
    create, getres = _mock_capsolver()
    with patch.object(pipeline.client, "create_task", create), \
         patch.object(pipeline.client, "get_task_result", getres), \
         patch.object(cdp_mod, "submit_widget_form", AsyncMock()), \
         patch.object(cdp_mod, "page_advanced", AsyncMock(return_value=True)):
        ctx = _ctx()
        res = await pipeline.run_solve(strat, _det(True), ctx, {})
    assert res.error is None
    assert "cleared" in (res.extracted_content or "").lower()
    assert ctx.cost_sink == [0.001]


async def test_pipeline_gives_up_after_two_interstitial_failures():
    strat = _FakeStrategy(interstitial=True)
    create, getres = _mock_capsolver(
        results=[{"status": "ready", "solution": {"token": "t"}, "cost": "0.001"}] * 4
    )
    giveups = {}
    with patch.object(pipeline.client, "create_task", create), \
         patch.object(pipeline.client, "get_task_result", getres), \
         patch.object(cdp_mod, "submit_widget_form", AsyncMock()), \
         patch.object(cdp_mod, "page_advanced", AsyncMock(return_value=False)), \
         patch.object(pipeline, "detect_captcha", AsyncMock(return_value=None)):
        await pipeline.run_solve(strat, _det(True), _ctx(), giveups)
        await pipeline.run_solve(strat, _det(True), _ctx(), giveups)
        third = await pipeline.run_solve(strat, _det(True), _ctx(), giveups)
    assert giveups["site.example"] == 2
    assert "refused" in (third.error or "").lower()


async def test_pipeline_records_cost_on_failure():
    strat = _FakeStrategy()
    create, getres = _mock_capsolver(
        results=[{"status": "failed", "errorDescription": "boom", "cost": "0.002"}]
    )
    ctx = _ctx()
    with patch.object(pipeline.client, "create_task", create), \
         patch.object(pipeline.client, "get_task_result", getres):
        res = await pipeline.run_solve(strat, _det(), ctx, {})
    assert res.error and "boom" in res.error
    assert ctx.cost_sink == [0.002]


async def test_pipeline_cost_cap_refuses():
    from dataclasses import replace as _replace
    strat = _FakeStrategy()
    ctx = _ctx(cost_sink=[0.04])
    capped = _replace(pipeline.settings, captcha_cost_cap_usd=0.03)
    with patch.object(pipeline, "settings", capped):
        res = await pipeline.run_solve(strat, _det(), ctx, {})
    assert "ceiling" in (res.error or "")
    assert "$0.03" in (res.error or "")


def _address_eval(base, here):
    async def fake_eval(session, expr):
        if "baseURI" in expr:
            return {"base": base, "here": here}
        return "agent/1.0"
    return fake_eval


async def _ctx_for(base, here, api_origin):
    from app.agent.captcha import tools as ctools
    with patch.object(ctools, "_eval_js", _address_eval(base, here)), \
         patch.object(ctools.cdp, "page_cookie_header", AsyncMock(return_value="")):
        return await ctools._build_ctx(
            SimpleNamespace(),
            Detection(kind="recaptcha_v2", params={"apiOrigin": api_origin}),
            None, [],
        )


async def test_a_rebase_matching_the_challenge_origin_is_believed():
    ctx = await _ctx_for("https://origin.example/verify",
                         "https://proxy.example/verify",
                         "https://origin.example")
    assert ctx.page_url == "https://origin.example/verify"
    assert ctx.host == "origin.example"


async def test_a_rebase_the_challenge_does_not_corroborate_is_ignored():
    ctx = await _ctx_for("https://attacker.example/",
                         "https://real.example/page",
                         "https://origin.example")
    assert ctx.page_url == "https://real.example/page"
    assert ctx.host == "real.example"


async def test_an_ordinary_page_uses_its_own_address():
    ctx = await _ctx_for("https://site.example/page",
                         "https://site.example/page", "")
    assert ctx.page_url == "https://site.example/page"


def test_captcha_spend_is_capped_by_default():
    """The default has to be a real ceiling, not a nominal one: a page names the
    address a solve is billed against, so a loop on a hostile page spends until
    something stops it."""
    from app.config import Settings
    cap = Settings().captcha_cost_cap_usd
    assert 0 < cap <= 0.05
    dearest_solve = 3.0 / 1000
    assert cap / dearest_solve >= 10


def test_every_token_strategy_can_actually_place_its_solution():
    from app.agent.captcha.base import TokenStrategy
    for s in all_strategies():
        if not isinstance(s, TokenStrategy):
            continue
        declares = bool(getattr(s, "response_fields", ()))
        overrides = (
            type(s)._place is not TokenStrategy._place
            or type(s).redeem is not TokenStrategy.redeem
        )
        assert declares or overrides, (
            f"{s.kind} would silently drop its solution: it names no response "
            "field and defines no placement of its own"
        )


def test_every_strategy_is_reachable_or_refused():
    """Nothing may create a paid task by a route that cannot apply the answer."""
    for s in all_strategies():
        reachable = s.kind in SAMPLE_PROBES or s.kind in HINT_ONLY
        assert reachable or s.unsupported_reason, (
            f"{s.kind} is reachable by neither a probe nor a declared hint and is "
            "not refused, so it can be billed for a solve it cannot apply"
        )


@pytest.mark.parametrize("kind", list(HINT_ONLY))
def test_hint_only_strategies_build_a_task_the_service_offers(kind):
    strat = strategy_for(kind)
    det = Detection(kind=kind, params=HINT_ONLY[kind])
    task = strat.build_task(det, _ctx())
    assert task["type"] in SERVICE_TASK_TYPES


async def test_a_hint_only_strategy_without_its_hint_spends_nothing():
    strat = strategy_for("imagetotext")
    ctx = _ctx()
    res = await pipeline.run_solve(strat, Detection(kind="imagetotext"), ctx, {})
    assert res.error and "answer_selector" in res.error
    assert ctx.cost_sink == []


def test_image_to_text_types_the_answer_into_the_named_field():
    strat = strategy_for("imagetotext")
    det = Detection(kind="imagetotext", params={"answer_selector": "#answer"})
    actions = strat.plan_actions({"text": "AB12"}, det)
    assert [(a.kind, a.selector, a.text) for a in actions] == [
        ("type", "#answer", "AB12")
    ]


async def test_aws_waf_token_becomes_a_cookie_and_a_reload():
    strat = strategy_for("awswaf_token")
    jar, reloaded, submitted = [], [], []
    with patch.object(cdp_mod, "set_cookies",
                      AsyncMock(side_effect=lambda s, c: jar.extend(c))), \
         patch.object(cdp_mod, "reload_page",
                      AsyncMock(side_effect=lambda s: reloaded.append(True))), \
         patch.object(cdp_mod, "submit_widget_form",
                      AsyncMock(side_effect=lambda *a: submitted.append(True))):
        await strat.redeem(
            {"token": "aws-waf-token=XYZ"},
            Detection(kind="awswaf_token", interstitial=True),
            _ctx(),
        )
    assert [(c["name"], c["value"]) for c in jar] == [("aws-waf-token", "XYZ")]
    assert reloaded == [True]
    assert submitted == []


def test_cookies_are_accepted_in_every_shape_a_solver_returns():
    from app.agent.captcha.cdp import normalise_cookies
    expected = [("a", "1", "site.example", "/"), ("b", "2", "site.example", "/")]
    for raw in ({"a": "1", "b": "2"},
                "a=1; b=2",
                [{"name": "a", "value": "1"}, {"name": "b", "value": "2"}]):
        got = normalise_cookies(raw, "site.example:443")
        assert [(c["name"], c["value"], c["domain"], c["path"]) for c in got] == expected
    assert normalise_cookies(None, "site.example") == []


def test_v3_uses_the_page_action_the_site_asked_for():
    strat = strategy_for("recaptcha_v3")
    named = strat.detect({"kind": "recaptcha_v3", "siteKey": "sk", "action": "login",
                          "confidence": 12})
    assert strat.build_task(named, _ctx())["pageAction"] == "login"
    bare = strat.detect({"kind": "recaptcha_v3", "siteKey": "sk", "confidence": 12})
    assert strat.build_task(bare, _ctx())["pageAction"] == "verify"


def test_probe_reads_the_score_action_off_the_page():
    js = probe_mod._PROBE_JS
    assert "scoreAction" in js
    assert "data-action" in js


async def test_a_retry_refuses_a_challenge_of_another_kind():
    strat = _FakeStrategy(interstitial=True)
    create, getres = _mock_capsolver(
        results=[{"status": "ready", "solution": {"token": "t"}, "cost": "0.001"}] * 4
    )
    other = Detection(kind="something-else", params={"dataS": "fresh"},
                      interstitial=True)
    with patch.object(pipeline.client, "create_task", create), \
         patch.object(pipeline.client, "get_task_result", getres), \
         patch.object(cdp_mod, "submit_widget_form", AsyncMock()), \
         patch.object(cdp_mod, "page_advanced", AsyncMock(return_value=False)), \
         patch.object(pipeline, "detect_captcha", AsyncMock(return_value=other)):
        res = await pipeline.run_solve(strat, _det(True), _ctx(), {})
    assert res.error and "still" in res.error
    assert create.await_count == 1


async def test_verify_does_not_wait_out_an_in_page_widget():
    from app.agent.captcha import base as base_mod
    budgets = []

    async def fake_advanced(session, timeout_s=25.0):
        budgets.append(timeout_s)
        return False

    with patch.object(base_mod.cdp, "page_advanced", fake_advanced):
        await _FakeStrategy().verify(_det(False), _ctx())
        await _FakeStrategy().verify(_det(True), _ctx())
    assert budgets[0] < budgets[1]


async def test_shared_placement_writes_into_the_widgets_form():
    from app.agent.captcha.base import TokenStrategy, _PLACE_JS
    seen = {}

    class _Probe(TokenStrategy):
        kind = "probe-only"
        solution_keys = ("token",)
        response_fields = ("x-response",)
        widget_selector = ".x-widget"

        def detect(self, probe):
            return None

        def build_task(self, det, ctx):
            return {}

    async def fake_eval(session, expr):
        seen["js"] = expr
        return {"fields": 1, "inForm": True, "valueLen": 9, "callback": "cb"}

    from app.agent.captcha import base as base_mod
    with patch.object(base_mod, "_eval_js", fake_eval):
        await _Probe()._place(SimpleNamespace(), {"token": "abcdefghi"},
                              Detection(kind="probe-only"))
    assert '"x-response"' in seen["js"]
    assert ".x-widget" in seen["js"]
    assert "form.querySelector" in _PLACE_JS


def test_token_is_placed_inside_the_widgets_form():
    from app.agent.captcha.base import _PLACE_JS
    assert 'closest("form")' in _PLACE_JS
    assert "make(form)" in _PLACE_JS
    assert "out.inForm" in _PLACE_JS


def test_submit_refuses_anything_but_this_challenges_own_filled_form():
    from app.agent.captcha.cdp import _SUBMIT_WIDGET_JS
    assert '"empty"' in _SUBMIT_WIDGET_JS
    assert '"no-response-field"' in _SUBMIT_WIDGET_JS
    assert 'document.querySelector("form")' not in _SUBMIT_WIDGET_JS


async def test_submit_is_told_this_challenges_own_response_field():
    seen = {}

    async def fake_eval(session, expr):
        seen["js"] = expr
        return "submitted"

    with patch.object(cdp_mod, "_eval_js", fake_eval):
        await cdp_mod.submit_widget_form(SimpleNamespace(), ("x-response",), ".x-widget")
    assert '"x-response"' in seen["js"]
    assert ".x-widget" in seen["js"]


async def test_page_advanced_needs_the_challenge_gone_not_a_new_url():
    settled = AsyncMock(return_value=True)
    still_there = AsyncMock(return_value={"kind": "recaptcha_v2"})
    with patch.object(cdp_mod, "_eval_js", settled), \
         patch.object(cdp_mod, "probe_strict", still_there):
        assert await cdp_mod.page_advanced(SimpleNamespace(), timeout_s=2.5) is False
    gone = AsyncMock(return_value=None)
    with patch.object(cdp_mod, "_eval_js", settled), \
         patch.object(cdp_mod, "probe_strict", gone):
        assert await cdp_mod.page_advanced(SimpleNamespace(), timeout_s=6.0) is True


async def test_page_advanced_ignores_a_single_clear_read_mid_load():
    settled = AsyncMock(return_value=True)
    flicker = AsyncMock(side_effect=[None, {"kind": "recaptcha_v2"},
                                     {"kind": "recaptcha_v2"}])
    with patch.object(cdp_mod, "_eval_js", settled), \
         patch.object(cdp_mod, "probe_strict", flicker):
        assert await cdp_mod.page_advanced(SimpleNamespace(), timeout_s=3.5) is False


def test_interstitial_detection_allows_a_widget_with_a_callback():
    js = probe_mod._PROBE_JS
    assert 'attr(widget, ["data-callback"])' not in js
    assert "creds" in js


def test_no_site_or_prompt_literals_in_captcha_package():
    root = pathlib.Path(__file__).resolve().parent.parent / "app" / "agent" / "captcha"
    banned = ("google", "/sorry/", "andy burnham", "newsfetcher", "diversify",
              "marshmallow", "ashby", "form#captcha-form")
    for path in root.rglob("*.py"):
        text = path.read_text().lower()
        for word in banned:
            assert word not in text, f"{word!r} found in {path.name}"


def test_probe_js_has_no_host_literals():
    js = probe_mod._PROBE_JS.lower()
    assert "google" not in js
    assert "/sorry/" not in js
