"""Static per-model pricing for LLM cost estimation.

Prices are USD per 1,000,000 tokens, pulled from Anthropic's published pricing
as of 2026-06-24. This table WILL drift as pricing changes — update the
entries and this date comment together when it does.
"""

MODEL_PRICING: dict[str, tuple[float, float]] = {
    # model_id -> (input $/Mtok, output $/Mtok)
    "claude-haiku-4-5-20251001": (1.00, 5.00),
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-opus-4-8": (5.00, 25.00),
}

# Cache reads run at ~0.1x the input rate; cache writes (5-minute TTL) at ~1.25x.
_CACHE_READ_MULTIPLIER = 0.1
_CACHE_WRITE_MULTIPLIER = 1.25


def estimate_cost_usd(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float | None:
    """Estimate USD cost for one LLM call. Returns None for an unpriced model."""
    pricing = MODEL_PRICING.get(model)
    if pricing is None:
        return None
    input_rate, output_rate = pricing
    return (
        input_tokens * input_rate
        + output_tokens * output_rate
        + cache_read_tokens * input_rate * _CACHE_READ_MULTIPLIER
        + cache_write_tokens * input_rate * _CACHE_WRITE_MULTIPLIER
    ) / 1_000_000
