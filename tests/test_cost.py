"""Cost-engine tests — per-provider tiered pricing from real token usage."""

from datetime import datetime, timezone
from types import SimpleNamespace

from app.agent import cost


def _usage(
    *,
    prompt_tokens=0,
    prompt_cached_tokens=0,
    prompt_cache_creation_tokens=0,
    prompt_cache_creation_5m_tokens=None,
    prompt_cache_creation_1h_tokens=None,
    completion_tokens=0,
    pricing_multiplier=None,
):
    return SimpleNamespace(
        prompt_tokens=prompt_tokens,
        prompt_cached_tokens=prompt_cached_tokens,
        prompt_cache_creation_tokens=prompt_cache_creation_tokens,
        prompt_cache_creation_5m_tokens=prompt_cache_creation_5m_tokens,
        prompt_cache_creation_1h_tokens=prompt_cache_creation_1h_tokens,
        completion_tokens=completion_tokens,
        pricing_multiplier=pricing_multiplier,
    )


_AUG = datetime(2026, 8, 10, tzinfo=timezone.utc)
_SEP = datetime(2026, 9, 2, tzinfo=timezone.utc)


def test_anthropic_opus_with_cache_and_output():
    u = _usage(
        prompt_tokens=1000,
        prompt_cached_tokens=100,
        prompt_cache_creation_tokens=500,
        completion_tokens=200,
    )
    c = cost.usage_cost("claude-opus-4-8", u, now=_AUG)
    assert c == (900 * 5 + 100 * 0.5 + 500 * 6.25 + 200 * 25) / 1_000_000
    assert round(c, 6) == 0.012675


def test_anthropic_pricing_multiplier_applied():
    u = _usage(prompt_tokens=1000, completion_tokens=0, pricing_multiplier=1.1)
    c = cost.usage_cost("claude-opus-4-8", u, now=_AUG)
    assert round(c, 9) == round(1000 * 5 / 1_000_000 * 1.1, 9)


def test_anthropic_split_5m_1h_cache_writes():
    u = _usage(
        prompt_tokens=100,
        prompt_cache_creation_tokens=300,
        prompt_cache_creation_5m_tokens=200,
        prompt_cache_creation_1h_tokens=100,
        completion_tokens=0,
    )
    c = cost.usage_cost("claude-opus-4-8", u, now=_AUG)
    assert c == (100 * 5 + 200 * 6.25 + 100 * 10) / 1_000_000


def test_openai_cached_and_cache_write():
    u = _usage(
        prompt_tokens=2006,
        prompt_cached_tokens=1920,
        prompt_cache_creation_tokens=0,
        completion_tokens=300,
    )
    c = cost.usage_cost("gpt-5.6-luna", u, now=_AUG)
    assert round(c, 10) == round((86 * 0.2 + 1920 * 0.02 + 300 * 1.2) / 1_000_000, 10)


def test_openai_cache_write_priced():
    u = _usage(
        prompt_tokens=1000,
        prompt_cached_tokens=200,
        prompt_cache_creation_tokens=300,
        completion_tokens=100,
    )
    c = cost.usage_cost("gpt-5.6-sol", u, now=_AUG)
    expected = (500 * 5 + 200 * 0.5 + 300 * 6.25 + 100 * 30) / 1_000_000
    assert round(c, 10) == round(expected, 10)


def test_openai_long_context_tier():
    short = cost.usage_cost("gpt-5.6-sol", _usage(prompt_tokens=100_000, completion_tokens=0), now=_AUG)
    long = cost.usage_cost("gpt-5.6-sol", _usage(prompt_tokens=300_000, completion_tokens=0), now=_AUG)
    assert round(short, 9) == round(100_000 * 5 / 1_000_000, 9)
    assert round(long, 9) == round(300_000 * 10 / 1_000_000, 9)


def test_sonnet_5_date_cutover():
    u = _usage(prompt_tokens=1000, completion_tokens=1000)
    before = cost.usage_cost("claude-sonnet-5", u, now=_AUG)
    after = cost.usage_cost("claude-sonnet-5", u, now=_SEP)
    assert round(before, 9) == round((1000 * 2 + 1000 * 10) / 1_000_000, 9)
    assert round(after, 9) == round((1000 * 3 + 1000 * 15) / 1_000_000, 9)


def test_gemini_flash_prices_implicit_cache_hits():
    u = _usage(
        prompt_tokens=1_000_000,
        prompt_cached_tokens=400_000,
        prompt_cache_creation_tokens=None,
        completion_tokens=10_000,
    )
    got = cost.usage_cost("gemini-3.7-flash", u, now=_AUG)
    expected = (600_000 * 0.75 + 400_000 * 0.075 + 10_000 * 3.75) / 1_000_000
    assert round(got, 9) == round(expected, 9)
    assert got > 0


def test_unknown_model_is_free():
    assert cost.usage_cost("some-unlisted-model", _usage(prompt_tokens=1000), now=_AUG) == 0.0


def test_history_cost_sums_entries():
    entries = [
        SimpleNamespace(model="claude-opus-4-8", usage=_usage(prompt_tokens=1000, completion_tokens=0)),
        SimpleNamespace(model="gpt-5.6-luna", usage=_usage(prompt_tokens=1000, completion_tokens=0)),
        SimpleNamespace(model="claude-opus-4-8", usage=None),
    ]
    total = cost.history_cost(entries, now=_AUG)
    assert round(total, 10) == round((1000 * 5 + 1000 * 0.2) / 1_000_000, 10)
