"""Cost-engine tests — per-provider tiered pricing from real token usage."""

from types import SimpleNamespace

from openbrowse.agent import cost


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




def test_anthropic_opus_with_cache_and_output():
    u = _usage(
        prompt_tokens=1000,
        prompt_cached_tokens=100,
        prompt_cache_creation_tokens=500,
        completion_tokens=200,
    )
    c = cost.usage_cost("claude-opus-4-8", u)
    assert c == (900 * 5 + 100 * 0.5 + 500 * 6.25 + 200 * 25) / 1_000_000
    assert round(c, 6) == 0.012675


def test_anthropic_pricing_multiplier_applied():
    u = _usage(prompt_tokens=1000, completion_tokens=0, pricing_multiplier=1.1)
    c = cost.usage_cost("claude-opus-4-8", u)
    assert round(c, 9) == round(1000 * 5 / 1_000_000 * 1.1, 9)


def test_anthropic_split_5m_1h_cache_writes():
    u = _usage(
        prompt_tokens=100,
        prompt_cache_creation_tokens=300,
        prompt_cache_creation_5m_tokens=200,
        prompt_cache_creation_1h_tokens=100,
        completion_tokens=0,
    )
    c = cost.usage_cost("claude-opus-4-8", u)
    assert c == (100 * 5 + 200 * 6.25 + 100 * 10) / 1_000_000


def test_openai_cached_and_cache_write():
    u = _usage(
        prompt_tokens=2006,
        prompt_cached_tokens=1920,
        prompt_cache_creation_tokens=0,
        completion_tokens=300,
    )
    c = cost.usage_cost("gpt-5.6-luna", u)
    assert round(c, 10) == round((86 * 0.2 + 1920 * 0.02 + 300 * 1.2) / 1_000_000, 10)


def test_openai_cache_write_priced():
    u = _usage(
        prompt_tokens=1000,
        prompt_cached_tokens=200,
        prompt_cache_creation_tokens=300,
        completion_tokens=100,
    )
    c = cost.usage_cost("gpt-5.6-sol", u)
    expected = (500 * 4 + 200 * 0.4 + 300 * 5.0 + 100 * 20) / 1_000_000
    assert round(c, 10) == round(expected, 10)


def test_openai_long_context_tier():
    short = cost.usage_cost("gpt-5.6-sol", _usage(prompt_tokens=100_000, completion_tokens=0))
    long = cost.usage_cost("gpt-5.6-sol", _usage(prompt_tokens=300_000, completion_tokens=0))
    assert round(short, 9) == round(100_000 * 4 / 1_000_000, 9)
    assert round(long, 9) == round(300_000 * 8 / 1_000_000, 9)


def test_sonnet_5_pricing():
    u = _usage(prompt_tokens=1000, completion_tokens=1000)
    priced = cost.usage_cost("claude-sonnet-5", u)
    assert round(priced, 9) == round((1000 * 2 + 1000 * 10) / 1_000_000, 9)


def test_opus_5_5_cache_reads_at_a_twentieth_of_input():
    u = _usage(
        prompt_tokens=1000,
        prompt_cached_tokens=400,
        prompt_cache_creation_5m_tokens=100,
        prompt_cache_creation_1h_tokens=50,
        prompt_cache_creation_tokens=150,
        completion_tokens=200,
    )
    c = cost.usage_cost("claude-opus-5-5", u)
    expected = (600 * 4 + 400 * 0.2 + 100 * 5 + 50 * 8 + 200 * 20) / 1_000_000
    assert round(c, 10) == round(expected, 10)


def test_gpt6_short_and_long_context_tiers():
    rates = {
        "gpt-6-astra": ((10, 1, 12.5, 50), (20, 2, 25, 75)),
        "gpt-6-sol": ((2, 0.2, 2.5, 10), (4, 0.4, 5, 15)),
        "gpt-6-luna": ((0.1, 0.01, 0.125, 0.5), (0.2, 0.02, 0.25, 0.75)),
    }
    for model, tiers in rates.items():
        for prompt, (inp, cached, write, out) in zip((200_000, 300_000), tiers):
            u = _usage(
                prompt_tokens=prompt,
                prompt_cached_tokens=10_000,
                prompt_cache_creation_tokens=5_000,
                completion_tokens=1_000,
            )
            expected = (
                (prompt - 15_000) * inp + 10_000 * cached + 5_000 * write + 1_000 * out
            ) / 1_000_000
            assert round(cost.usage_cost(model, u), 10) == round(expected, 10), (model, prompt)


def test_fable_5_1_cache_reads_at_a_fortieth_of_input():
    u = _usage(prompt_tokens=1000, prompt_cached_tokens=1000, completion_tokens=0)
    assert round(cost.usage_cost("claude-fable-5-1", u), 10) == round(1000 * 0.25 / 1_000_000, 10)


def _attempt(kind, model, inp, out, read=0, write=0):
    return {
        "type": kind,
        "model": model,
        "input_tokens": inp,
        "output_tokens": out,
        "cache_read_input_tokens": read,
        "cache_creation_input_tokens": write,
    }


def test_fallback_cost_skips_the_unbilled_pre_output_decline():
    message = SimpleNamespace(
        model="claude-opus-5",
        content=[],
        usage=SimpleNamespace(
            iterations=[
                _attempt("message", "claude-opus-5-5", 5000, 0),
                _attempt("fallback_message", "claude-opus-5", 1000, 100, read=4000),
            ]
        ),
    )
    got = cost.fallback_cost("claude-opus-5-5", message)
    assert round(got, 10) == round((1000 * 5 + 4000 * 0.5 + 100 * 25) / 1_000_000, 10)


def test_fallback_cost_bills_a_mid_output_decline_at_its_own_rates():
    message = SimpleNamespace(
        model="claude-opus-5",
        content=[],
        usage=SimpleNamespace(
            iterations=[
                _attempt("message", "claude-opus-5-5", 1000, 50),
                _attempt("fallback_message", "claude-opus-5", 1000, 100),
            ]
        ),
    )
    got = cost.fallback_cost("claude-opus-5-5", message)
    expected = (1000 * 4 + 50 * 20 + 1000 * 5 + 100 * 25) / 1_000_000
    assert round(got, 10) == round(expected, 10)


def test_fallback_cost_without_iterations_reads_the_fallback_block():
    message = SimpleNamespace(
        model="claude-opus-5-5",
        content=[{"type": "fallback", "from": {"model": "claude-opus-5-5"}, "to": {"model": "claude-opus-5"}}],
        usage=SimpleNamespace(
            input_tokens=1000, output_tokens=100, cache_read_input_tokens=0,
            cache_creation_input_tokens=0, cache_creation=None,
        ),
    )
    got = cost.fallback_cost("claude-opus-5-5", message)
    assert round(got, 10) == round((1000 * 5 + 100 * 25) / 1_000_000, 10)


def test_fallback_cost_is_none_when_the_requested_model_served():
    message = SimpleNamespace(
        model="claude-opus-5-5",
        content=[{"type": "text", "text": "ok"}],
        usage=SimpleNamespace(input_tokens=10, output_tokens=5, iterations=None),
    )
    assert cost.fallback_cost("claude-opus-5-5", message) is None
    sticky_free = SimpleNamespace(
        model="claude-opus-5-5",
        content=[],
        usage=SimpleNamespace(iterations=[_attempt("message", "claude-opus-5-5", 10, 5)]),
    )
    assert cost.fallback_cost("claude-opus-5-5", sticky_free) is None


def test_unknown_model_is_free():
    assert cost.usage_cost("some-unlisted-model", _usage(prompt_tokens=1000)) == 0.0


def test_history_cost_sums_entries():
    entries = [
        SimpleNamespace(model="claude-opus-4-8", usage=_usage(prompt_tokens=1000, completion_tokens=0)),
        SimpleNamespace(model="gpt-5.6-luna", usage=_usage(prompt_tokens=1000, completion_tokens=0)),
        SimpleNamespace(model="claude-opus-4-8", usage=None),
    ]
    total = cost.history_cost(entries)
    assert round(total, 10) == round((1000 * 5 + 1000 * 0.2) / 1_000_000, 10)
