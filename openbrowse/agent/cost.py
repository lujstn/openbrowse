"""Real-cost engine — USD cost from per-call token usage, priced per provider.

Prompt caching is provider-managed and always on: browser-use marks the system prompt and the
latest state message as ``cache=True`` for Anthropic (prefix caching) and the structured-output
tool schema is cached unconditionally, while OpenAI caches automatically server-side. Tool schemas,
tool_use/tool_result blocks, fetched page content, python-sandbox output, and screenshot vision
tokens are all counted inside the API-returned token totals, so costing from real usage prices every
tool and fetch at model rates with nothing extra to add.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

_MTOK = 1_000_000
_OPENAI_LONG_THRESHOLD = 272_000


@dataclass(frozen=True)
class Price:
    """Per-token USD rates for one pricing tier."""

    input: float
    cache_read: float
    cache_write_5m: float
    cache_write_1h: float
    output: float


@dataclass(frozen=True)
class ModelPricing:
    standard: Price
    long: Price | None = None
    long_threshold: int | None = None


def _p(inp: float, cache_read: float, cw5m: float, cw1h: float, out: float) -> Price:
    return Price(
        input=inp / _MTOK,
        cache_read=cache_read / _MTOK,
        cache_write_5m=cw5m / _MTOK,
        cache_write_1h=cw1h / _MTOK,
        output=out / _MTOK,
    )


_PRICING: dict[str, ModelPricing] = {
    "claude-fable-5": ModelPricing(_p(10, 1, 12.5, 20, 50)),
    "claude-mythos-5": ModelPricing(_p(10, 1, 12.5, 20, 50)),
    "claude-opus-5": ModelPricing(_p(5, 0.5, 6.25, 10, 25)),
    "claude-opus-4-8": ModelPricing(_p(5, 0.5, 6.25, 10, 25)),
    "claude-opus-4-7": ModelPricing(_p(5, 0.5, 6.25, 10, 25)),
    "claude-opus-4-6": ModelPricing(_p(5, 0.5, 6.25, 10, 25)),
    "claude-sonnet-5": ModelPricing(_p(2, 0.2, 2.5, 4, 10)),
    "claude-sonnet-4-6": ModelPricing(_p(3, 0.3, 3.75, 6, 15)),
    "gpt-5.6-sol": ModelPricing(
        standard=_p(4, 0.4, 5.0, 0, 20),
        long=_p(8, 0.8, 10.0, 0, 30),
        long_threshold=_OPENAI_LONG_THRESHOLD,
    ),
    "gpt-5.6-terra": ModelPricing(
        standard=_p(2, 0.2, 2.5, 0, 12),
        long=_p(4, 0.4, 5.0, 0, 18),
        long_threshold=_OPENAI_LONG_THRESHOLD,
    ),
    "gpt-5.6-luna": ModelPricing(
        standard=_p(0.2, 0.02, 0.25, 0, 1.2),
        long=_p(0.4, 0.04, 0.5, 0, 1.8),
        long_threshold=_OPENAI_LONG_THRESHOLD,
    ),
}


def _lookup(model_id: str) -> ModelPricing | None:
    return _PRICING.get(model_id)


def _is_openai(model_id: str) -> bool:
    return model_id.startswith(("gpt", "o1", "o3", "o4", "chatgpt"))


def usage_cost(model_id: str, usage: Any) -> float:
    """USD cost of a single model invocation from its real token usage."""
    pricing = _lookup(model_id)
    if pricing is None:
        logger.warning("No price table for model %r; counting its cost as 0", model_id)
        return 0.0

    prompt_tokens = usage.prompt_tokens or 0
    cached = usage.prompt_cached_tokens or 0
    cache_write = usage.prompt_cache_creation_tokens or 0
    output = usage.completion_tokens or 0
    multiplier = usage.pricing_multiplier or 1.0

    price = pricing.standard
    if (
        pricing.long is not None
        and pricing.long_threshold is not None
        and prompt_tokens > pricing.long_threshold
    ):
        price = pricing.long

    if _is_openai(model_id):
        uncached = max(0, prompt_tokens - cached - cache_write)
        cost = (
            uncached * price.input
            + cached * price.cache_read
            + cache_write * price.cache_write_5m
            + output * price.output
        )
    else:
        uncached = max(0, prompt_tokens - cached)
        cw5m = usage.prompt_cache_creation_5m_tokens
        cw1h = usage.prompt_cache_creation_1h_tokens
        if cw5m is None and cw1h is None:
            cw5m_tok, cw1h_tok = cache_write, 0
        else:
            cw5m_tok = cw5m or 0
            cw1h_tok = cw1h or 0
        cost = (
            uncached * price.input
            + cached * price.cache_read
            + cw5m_tok * price.cache_write_5m
            + cw1h_tok * price.cache_write_1h
            + output * price.output
        )

    return cost * multiplier


def history_cost(usage_history: Any) -> float:
    """Total USD cost across a token_cost_service.usage_history list."""
    total = 0.0
    for entry in usage_history or []:
        if getattr(entry, "usage", None) is None:
            continue
        total += usage_cost(entry.model, entry.usage)
    return total
