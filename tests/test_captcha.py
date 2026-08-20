"""Tests for the captcha subsystem: strategies, dispatch, pipeline, hygiene."""

from __future__ import annotations

import pathlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from openbrowse.agent.captcha import all_strategies, strategy_for
from openbrowse.agent.captcha.base import Detection, SolveContext, TokenStrategy
from openbrowse.agent.captcha.registry import detect_from_probe
from openbrowse.agent.captcha import cdp as cdp_mod, pipeline, probe as probe_mod


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
    from openbrowse.agent.captcha.tools import register_captcha_tools
    with patch("openbrowse.agent.captcha.tools.settings") as s:
        s.capsolver_api_key = "k"
        tools = Tools()
        register_captcha_tools(tools, [], None)
        assert "solve_captcha" in tools.registry.registry.actions


def test_solve_captcha_without_key_speaks_only_when_faced():
    """No CAPSOLVER_API_KEY must not banner every session at start; the fact
    solving is off matters only at the moment a challenge actually appears,
    so a stub action carries the message instead."""
    import asyncio

    from browser_use import Tools

    from openbrowse.agent.captcha.tools import register_captcha_tools

    with patch("openbrowse.agent.captcha.tools.settings") as s:
        s.capsolver_api_key = ""
        tools = Tools()
        seen: list[str] = []

        async def progress(message: str) -> None:
            seen.append(message)

        register_captcha_tools(tools, [], progress)
        action = tools.registry.registry.actions["solve_captcha"]
        result = asyncio.run(action.function())
        assert result.error and "CAPSOLVER_API_KEY" in result.error
        assert seen and "CAPTCHA solving is off" in seen[0]

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


def test_runtime_sources_are_part_of_the_page_probe():
    js = probe_mod._PROBE_JS
    assert "window.mtcaptchaConfig" in js
    assert "window.mtcaptcha.getConfiguration" in js
    assert '__openbrowseCaptchaBridge' in js
    assert 'runtimeParam(["gt"])' in js
    assert 'runtimeParam(["challenge"])' in js
    assert 'runtimeParam(["captcha_id", "captchaId"])' in js
    assert js.index('out.challenge = runtimeParam(["challenge"])') < js.index(
        "gt3.challenge"
    )
    assert js.index("gt4.captchaId") < js.index(
        'attr(gtEl, ["data-captcha-id"])'
    )
    assert js.index("mtConfig.sitekey") < js.index(
        'attr(mt, ["data-mtcaptcha-sitekey", "data-sitekey"])'
    )


def test_recognised_widgets_survive_missing_runtime_parameters():
    v3 = strategy_for("geetest_v3").detect(
        {"kind": "geetest_v3", "gt": "", "challenge": "", "confidence": 15}
    )
    v4 = strategy_for("geetest_v4").detect(
        {"kind": "geetest_v4", "captchaId": "", "confidence": 15}
    )
    mt = strategy_for("mtcaptcha").detect(
        {"kind": "mtcaptcha", "siteKey": "", "confidence": 15}
    )
    assert v3 is not None and v3.params["gt"] == ""
    assert v4 is not None and v4.params["captchaId"] == ""
    assert mt is not None and mt.params["siteKey"] == ""
    assert detect_from_probe(
        {"kind": "geetest_v3", "gt": "", "challenge": "", "confidence": 15}
    ).kind == "geetest_v3"


@pytest.mark.parametrize(
    ("kind", "params", "missing"),
    [
        ("geetest_v3", {"gt": "", "challenge": ""}, "gt and challenge"),
        ("geetest_v4", {"captchaId": ""}, "captchaId"),
        ("mtcaptcha", {"siteKey": ""}, "siteKey"),
    ],
)
async def test_missing_runtime_parameters_never_create_a_paid_task(
    kind, params, missing
):
    strat = strategy_for(kind)
    create = AsyncMock()
    with patch.object(pipeline.client, "create_task", create):
        result = await pipeline.run_solve(
            strat,
            Detection(kind=kind, params=params, confidence=15),
            _ctx(),
            {},
        )
    assert missing in (result.error or "")
    assert "recognised" in (result.error or "")
    create.assert_not_awaited()


async def test_geetest_v3_refreshes_runtime_parameters_before_the_task():
    from openbrowse.agent.captcha.strategies import geetest as geetest_mod

    strat = strategy_for("geetest_v3")
    fresh = {
        "gt": "fresh-gt",
        "challenge": "fresh-challenge",
        "geetestApiServer": "api.example",
    }
    with patch.object(geetest_mod, "_eval_js", AsyncMock(return_value=fresh)):
        captured = await strat.capture(
            Detection(kind="geetest_v3", params={"gt": "old", "challenge": "old"}),
            _ctx(),
        )
    assert captured == {
        "gt": "fresh-gt",
        "challenge": "fresh-challenge",
        "geetestApiServer": "api.example",
    }


@pytest.mark.parametrize(
    ("kind", "solution", "expected"),
    [
        (
            "geetest_v3",
            {"challenge": "c", "validate": "v", "seccode": "s"},
            ("geetest_challenge", "geetest_validate", "geetest_seccode"),
        ),
        (
            "geetest_v4",
            {
                "captcha_id": "cid",
                "lot_number": "lot",
                "pass_token": "pass",
                "gen_time": "time",
                "captcha_output": "out",
            },
            ("captcha_id", "lot_number", "pass_token", "gen_time", "captcha_output"),
        ),
    ],
)
async def test_geetest_solution_uses_getvalidate_fields_and_success_callbacks(
    kind, solution, expected
):
    from openbrowse.agent.captcha.strategies import geetest as geetest_mod

    seen = {}

    async def fake_eval(session, expression):
        seen["js"] = expression
        return {"fields": len(expected), "callbacks": 1, "instance": True}

    with patch.object(geetest_mod, "_eval_js", fake_eval):
        await strategy_for(kind)._place(SimpleNamespace(), solution, Detection(kind=kind))
    for field in expected:
        assert field in seen["js"]
    assert "instance.getValidate" in seen["js"]
    assert "callbacks[c].call(instance)" in seen["js"]


async def test_mtcaptcha_supports_named_and_function_callbacks():
    from openbrowse.agent.captcha.strategies import mtcaptcha as mtcaptcha_mod

    seen = {}

    async def fake_eval(session, expression):
        seen["js"] = expression

    with patch.object(mtcaptcha_mod, "_eval_js", fake_eval):
        await strategy_for("mtcaptcha")._after_place(
            SimpleNamespace(), "verified-token", Detection(kind="mtcaptcha")
        )
    assert "typeof cb==='string'" in seen["js"]
    assert "verifiedToken:t" in seen["js"]
    assert "isVerified:true" in seen["js"]
    assert strategy_for("mtcaptcha").response_fields == ("mtcaptcha-verifiedtoken",)


async def test_bridge_is_installed_for_the_current_and_next_document():
    from openbrowse.agent.captcha import bridge as bridge_mod

    add_script = AsyncMock(return_value={"identifier": "bridge"})
    evaluate = AsyncMock(return_value={})
    send = SimpleNamespace(
        Page=SimpleNamespace(addScriptToEvaluateOnNewDocument=add_script),
        Runtime=SimpleNamespace(evaluate=evaluate),
    )
    cdp_session = SimpleNamespace(
        cdp_client=SimpleNamespace(send=send), session_id="session-1"
    )
    browser = SimpleNamespace(
        get_or_create_cdp_session=AsyncMock(return_value=cdp_session)
    )
    await bridge_mod.install_captcha_bridge(browser, "target-1")
    browser.get_or_create_cdp_session.assert_awaited_once_with(
        "target-1", focus=False
    )
    add_script.assert_awaited_once()
    evaluate.assert_awaited_once()


def test_bridge_preserves_initialisers_and_chainable_success_registration():
    from openbrowse.agent.captcha.bridge import _BRIDGE_JS

    assert 'return original.apply(this, args)' in _BRIDGE_JS
    assert 'return original.apply(this, arguments)' in _BRIDGE_JS
    assert 'slot.success.push(callback)' in _BRIDGE_JS
    assert 'configurable: true' in _BRIDGE_JS


def test_bridge_captures_and_replays_the_page_geetest_registration_get():
    from openbrowse.agent.captcha.bridge import _BRIDGE_JS

    assert 'request.method !== "GET"' in _BRIDGE_JS
    assert "response.clone().text()" in _BRIDGE_JS
    assert "current.challenge === config.challenge" in _BRIDGE_JS
    assert "state.refreshGeetestV3 = async function" in _BRIDGE_JS
    assert 'cache: "no-store"' in _BRIDGE_JS


async def test_geetest_v3_without_a_replayable_challenge_spends_nothing():
    from openbrowse.agent.captcha.strategies import geetest as geetest_mod

    strat = strategy_for("geetest_v3")
    create = AsyncMock()
    with patch.object(geetest_mod, "_eval_js", AsyncMock(return_value=None)), \
         patch.object(pipeline.client, "create_task", create):
        result = await pipeline.run_solve(
            strat,
            Detection(kind="geetest_v3", params={"gt": "g", "challenge": "used"}),
            _ctx(),
            {},
        )
    assert "could not be refreshed" in (result.error or "")
    assert "nothing was spent" in (result.error or "").lower()
    create.assert_not_awaited()


async def test_geetest_v3_refreshes_again_after_a_stale_challenge_error():
    from openbrowse.agent.captcha.strategies import geetest as geetest_mod

    strat = strategy_for("geetest_v3")
    refreshed = AsyncMock(side_effect=[
        {"gt": "g", "challenge": "fresh-1"},
        {"gt": "g", "challenge": "fresh-2"},
    ])
    create = AsyncMock(side_effect=[
        {"errorId": 1, "errorDescription": "old challenge, error_code: error_02"},
        {"solution": {"challenge": "c", "validate": "v", "seccode": "s"}},
    ])
    with patch.object(geetest_mod, "_eval_js", refreshed), \
         patch.object(strat, "_place", AsyncMock()), \
         patch.object(pipeline.client, "create_task", create):
        result = await pipeline.run_solve(
            strat,
            Detection(kind="geetest_v3", params={"gt": "g", "challenge": "used"}),
            _ctx(),
            {},
        )
    assert result.error is None
    assert create.await_args_list[0].args[1]["challenge"] == "fresh-1"
    assert create.await_args_list[1].args[1]["challenge"] == "fresh-2"


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
    from openbrowse.agent.captcha import tools as ctools
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
    from openbrowse.config import Settings
    cap = Settings().captcha_cost_cap_usd
    assert 0 < cap <= 0.05
    dearest_solve = 3.0 / 1000
    assert cap / dearest_solve >= 10


def test_every_token_strategy_can_actually_place_its_solution():
    from openbrowse.agent.captcha.base import TokenStrategy
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
    from openbrowse.agent.captcha.cdp import normalise_cookies
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
    from openbrowse.agent.captcha import base as base_mod
    budgets = []

    async def fake_advanced(session, timeout_s=25.0):
        budgets.append(timeout_s)
        return False

    with patch.object(base_mod.cdp, "page_advanced", fake_advanced):
        await _FakeStrategy().verify(_det(False), _ctx())
        await _FakeStrategy().verify(_det(True), _ctx())
    assert budgets[0] < budgets[1]


async def test_shared_placement_writes_into_the_widgets_form():
    from openbrowse.agent.captcha.base import TokenStrategy, _PLACE_JS
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

    from openbrowse.agent.captcha import base as base_mod
    with patch.object(base_mod, "_eval_js", fake_eval):
        await _Probe()._place(SimpleNamespace(), {"token": "abcdefghi"},
                              Detection(kind="probe-only"))
    assert '"x-response"' in seen["js"]
    assert ".x-widget" in seen["js"]
    assert "form.querySelector" in _PLACE_JS


def test_token_is_placed_inside_the_widgets_form():
    from openbrowse.agent.captcha.base import _PLACE_JS
    assert 'closest("form")' in _PLACE_JS
    assert "make(form)" in _PLACE_JS
    assert "out.inForm" in _PLACE_JS


def test_submit_refuses_anything_but_this_challenges_own_filled_form():
    from openbrowse.agent.captcha.cdp import _SUBMIT_WIDGET_JS
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
