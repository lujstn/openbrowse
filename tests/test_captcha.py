"""Tests for the captcha subsystem: strategies, dispatch, pipeline, hygiene."""

from __future__ import annotations

import pathlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.agent.captcha import all_strategies, strategy_for
from app.agent.captcha.base import Detection, SolveContext, TokenStrategy
from app.agent.captcha.registry import detect_from_probe
from app.agent.captcha import pipeline, probe as probe_mod


SAMPLE_PROBES = {
    "recaptcha_v2": {"kind": "recaptcha_v2", "siteKey": "sk", "dataS": "ds",
                     "apiOrigin": "https://widget.example", "confidence": 20},
    "recaptcha_v2_enterprise": {"kind": "recaptcha_v2_enterprise", "siteKey": "sk",
                                "dataS": "ds", "apiOrigin": "https://widget.example",
                                "confidence": 20},
    "recaptcha_v3": {"kind": "recaptcha_v3", "siteKey": "sk",
                     "apiOrigin": "https://widget.example", "confidence": 12},
    "hcaptcha": {"kind": "hcaptcha", "siteKey": "sk", "confidence": 20},
    "turnstile": {"kind": "turnstile", "siteKey": "sk", "confidence": 20},
    "geetest_v3": {"kind": "geetest_v3", "gt": "g", "challenge": "c", "confidence": 15},
    "geetest_v4": {"kind": "geetest_v4", "captchaId": "cid", "confidence": 15},
    "mtcaptcha": {"kind": "mtcaptcha", "siteKey": "sk", "confidence": 15},
    "datadome": {"kind": "datadome", "captchaUrl": "https://x/captcha", "confidence": 15},
    "awswaf_token": {"kind": "awswaf_token", "confidence": 12},
    "awswaf_image": {"kind": "awswaf_image", "question": "q", "confidence": 12},
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
        proxy="",
    )
    base.update(kw)
    return SolveContext(**base)


def test_solve_captcha_action_registers():
    from browser_use import Tools
    from app.agent.captcha.tools import register_captcha_tools
    with patch("app.agent.captcha.tools.settings") as s:
        s.capsolver_api_key = "k"
        s.captcha_proxy = ""
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
    assert len(kinds) >= 15


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

    def __init__(self, interstitial=False, requires_proxy=False):
        self._interstitial = interstitial
        self.requires_proxy = requires_proxy
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
         patch.object(pipeline.cdp, "submit_widget_form", AsyncMock()), \
         patch.object(pipeline.cdp, "page_advanced", AsyncMock(return_value=True)):
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
         patch.object(pipeline.cdp, "submit_widget_form", AsyncMock()), \
         patch.object(pipeline.cdp, "page_advanced", AsyncMock(return_value=False)), \
         patch.object(pipeline, "detect_captcha", AsyncMock(return_value=None)):
        await pipeline.run_solve(strat, _det(True), _ctx(), giveups)
        await pipeline.run_solve(strat, _det(True), _ctx(), giveups)
        third = await pipeline.run_solve(strat, _det(True), _ctx(), giveups)
    assert giveups["site.example"] == 2
    assert "refused" in (third.error or "").lower()


async def test_pipeline_proxy_gap_is_honest():
    strat = _FakeStrategy(requires_proxy=True)
    res = await pipeline.run_solve(strat, _det(), _ctx(proxy=""), {})
    assert "proxy" in (res.error or "").lower()
    assert "unwinnable" not in (res.error or "").lower()


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
    ctx = _ctx(cost_sink=[9.0])
    capped = _replace(pipeline.settings, captcha_cost_cap_usd=1.0)
    with patch.object(pipeline, "settings", capped):
        res = await pipeline.run_solve(strat, _det(), ctx, {})
    assert "cap" in (res.error or "").lower()


async def test_solve_context_names_the_documents_base_address():
    from app.agent.captcha import tools as ctools
    seen = []

    async def fake_eval(session, expr):
        seen.append(expr)
        return "https://origin.example/verify"

    with patch.object(ctools, "_eval_js", fake_eval), \
         patch.object(ctools.cdp, "page_cookie_header", AsyncMock(return_value="")):
        ctx = await ctools._build_ctx(
            SimpleNamespace(), Detection(kind="recaptcha_v2"), None, []
        )
    assert any("baseURI" in e for e in seen)
    assert ctx.page_url == "https://origin.example/verify"
    assert ctx.host == "origin.example"


def test_token_is_placed_inside_the_widgets_form():
    from app.agent.captcha.strategies.recaptcha import _PLACE_JS
    assert 'closest("form")' in _PLACE_JS
    assert "form.appendChild(ta)" in _PLACE_JS
    assert "out.inForm" in _PLACE_JS


def test_submit_refuses_an_empty_response_field():
    from app.agent.captcha.cdp import _SUBMIT_WIDGET_JS
    assert '"empty"' in _SUBMIT_WIDGET_JS
    assert "g-recaptcha-response" in _SUBMIT_WIDGET_JS


async def test_page_advanced_needs_the_challenge_gone_not_a_new_url():
    from app.agent.captcha import cdp as cdp_mod
    settled = AsyncMock(return_value=True)
    still_there = AsyncMock(return_value={"kind": "recaptcha_v2"})
    with patch.object(cdp_mod, "_eval_js", settled), \
         patch.object(cdp_mod, "probe_strict", still_there):
        assert await cdp_mod.page_advanced(SimpleNamespace(), "before", timeout_s=2.5) is False
    gone = AsyncMock(return_value=None)
    with patch.object(cdp_mod, "_eval_js", settled), \
         patch.object(cdp_mod, "probe_strict", gone):
        assert await cdp_mod.page_advanced(SimpleNamespace(), "before", timeout_s=6.0) is True


async def test_page_advanced_ignores_a_single_clear_read_mid_load():
    from app.agent.captcha import cdp as cdp_mod
    settled = AsyncMock(return_value=True)
    flicker = AsyncMock(side_effect=[None, {"kind": "recaptcha_v2"},
                                     {"kind": "recaptcha_v2"}])
    with patch.object(cdp_mod, "_eval_js", settled), \
         patch.object(cdp_mod, "probe_strict", flicker):
        assert await cdp_mod.page_advanced(SimpleNamespace(), "before", timeout_s=3.5) is False


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
