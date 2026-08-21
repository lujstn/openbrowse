"""maxCostUsd must be a ceiling, not a stop-loss. The step-boundary check
alone let a $0.25 run record $0.31: a judge review plus one step landed
between checks. The guard sits in the LLM wrappers, rechecks after every call
(bounding overshoot to one call) and reserves each upcoming call's worst case
before dispatching it (making overshoot impossible when the model is priced)."""

from types import SimpleNamespace

import pytest

from openbrowse.agent import cost
from openbrowse.agent.runner import (
    BudgetExceededError,
    _BudgetGuard,
    _estimate_request_tokens,
)

MODEL = "gpt-5.6-terra"


def _usage(prompt: int, completion: int) -> SimpleNamespace:
    return SimpleNamespace(
        prompt_tokens=prompt,
        prompt_cached_tokens=0,
        prompt_cache_creation_tokens=0,
        prompt_cache_creation_5m_tokens=None,
        prompt_cache_creation_1h_tokens=None,
        completion_tokens=completion,
        pricing_multiplier=1.0,
    )


def _service(*entries: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(
        usage_history=[SimpleNamespace(model=MODEL, usage=u) for u in entries]
    )


def _guard(budget: float, *history: SimpleNamespace) -> _BudgetGuard:
    guard = _BudgetGuard(
        MODEL, {"llm": 0.0, "capsolver": 0.0}, [], budget
    )
    guard.bind(_service(*history))
    return guard


# ── worst-case pricing ───────────────────────────────────────────────────────


def test_worst_case_cost_is_positive_for_priced_models() -> None:
    worst = cost.worst_case_call_cost(MODEL, 10_000, 4_096)
    assert worst is not None and worst > 0


def test_worst_case_cost_is_none_for_unpriced_models() -> None:
    assert cost.worst_case_call_cost("made-up-model", 10_000, 4_096) is None


def test_worst_case_charges_input_at_the_dearest_rate() -> None:
    pricing = cost._lookup("claude-sonnet-5")
    assert pricing is not None
    worst = cost.worst_case_call_cost("claude-sonnet-5", 1_000_000, 0)
    dearest = max(
        pricing.standard.input,
        pricing.standard.cache_write_5m,
        pricing.standard.cache_write_1h,
    )
    assert worst == pytest.approx(1_000_000 * dearest)


# ── request-size estimation ──────────────────────────────────────────────────


def test_text_estimates_overshoot_real_token_counts() -> None:
    # ~4 chars per English token; charging 3 keeps the estimate conservative.
    assert _estimate_request_tokens("word " * 300) >= 300


def test_base64_blobs_count_as_images_not_text() -> None:
    blob = "A" * 200_000
    est = _estimate_request_tokens([{"type": "image", "data": blob}])
    # The flat image allowance plus the few chars of surrounding structure —
    # nowhere near the ~67k tokens chars/3 would charge for the raw base64.
    assert 2000 <= est < 2100, est


def test_nested_message_structures_are_walked() -> None:
    payload = [{"content": [{"text": "x" * 300}, {"text": "y" * 300}]}]
    assert _estimate_request_tokens(payload) == 200


# ── the guard ────────────────────────────────────────────────────────────────


def test_postcheck_trips_the_moment_spend_reaches_the_cap() -> None:
    guard = _guard(0.25, _usage(1_000_000, 50_000))
    with pytest.raises(BudgetExceededError):
        guard.postcheck(_usage(1_000_000, 50_000))


def test_postcheck_counts_the_unregistered_call_it_just_saw() -> None:
    guard = _guard(0.25)
    assert guard.spent() == 0.0
    big = _usage(20_000_000, 100_000)
    with pytest.raises(BudgetExceededError):
        guard.postcheck(big)


def test_postcheck_passes_under_the_cap() -> None:
    guard = _guard(0.25)
    guard.postcheck(_usage(1_000, 100))


def test_precheck_refuses_a_call_that_could_cross_the_cap() -> None:
    guard = _guard(0.25, _usage(1_000_000, 40_000))
    with pytest.raises(BudgetExceededError) as exc:
        guard.precheck("x" * 4_000_000, 4_096)
    assert "before it is made" in str(exc.value)


def test_precheck_allows_a_call_that_fits() -> None:
    guard = _guard(5.00, _usage(1_000, 100))
    guard.precheck("a short prompt", 4_096)


def test_precheck_is_inert_without_budget_or_binding() -> None:
    unbudgeted = _BudgetGuard(MODEL, {"llm": 0.0, "capsolver": 0.0}, [], None)
    unbudgeted.bind(_service())
    unbudgeted.precheck("x" * 4_000_000, 4_096)

    unbound = _BudgetGuard(MODEL, {"llm": 0.0, "capsolver": 0.0}, [], 0.01)
    unbound.precheck("x" * 4_000_000, 4_096)


def test_precheck_is_inert_for_unpriced_models() -> None:
    guard = _BudgetGuard(
        "made-up-model", {"llm": 0.0, "capsolver": 0.0}, [], 0.01
    )
    guard.bind(_service())
    guard.precheck("x" * 4_000_000, 4_096)


def test_spent_includes_carried_and_capsolver_costs() -> None:
    guard = _BudgetGuard(
        MODEL, {"llm": 0.10, "capsolver": 0.02}, [0.03], 1.0
    )
    guard.bind(_service())
    assert guard.spent() == pytest.approx(0.15)
