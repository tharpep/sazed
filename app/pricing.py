"""Anthropic API list prices, USD per 1,000,000 tokens, as of PRICES_AS_OF."""

from dataclasses import dataclass

PRICES_AS_OF = "2026-11-06"


@dataclass(frozen=True)
class Rate:
    input: float
    output: float
    cache_read_multiplier: float = 0.1


MODEL_PRICING: dict[str, Rate] = {
    "claude-fable-5-1": Rate(10.00, 50.00, 0.025),
    "claude-fable-5": Rate(10.00, 50.00),
    "claude-opus-5-5": Rate(4.00, 20.00, 0.05),
    "claude-opus-5": Rate(5.00, 25.00),
    "claude-opus-4-8": Rate(5.00, 25.00),
    "claude-sonnet-5-5": Rate(2.00, 10.00, 0.05),
    "claude-sonnet-5": Rate(2.00, 10.00),
    "claude-sonnet-4-6": Rate(3.00, 15.00),
    "claude-haiku-5-5": Rate(0.10, 0.50),
    "claude-haiku-4-5-20251001": Rate(1.00, 5.00),
    "claude-haiku-4-5": Rate(1.00, 5.00),
}
LONG_PROMPT_PRICING: dict[str, tuple[int, Rate]] = {
    "claude-haiku-5-5": (100_000, Rate(0.50, 2.50)),
}
FAST_PRICING: dict[str, Rate] = {
    "claude-opus-5-5": Rate(8.00, 40.00, 0.05),
    "claude-opus-5": Rate(10.00, 50.00),
    "claude-opus-4-8": Rate(10.00, 50.00),
}
LONG_PROMPT_TOKENS = min(threshold for threshold, _ in LONG_PROMPT_PRICING.values())
CACHE_WRITE_5M_MULTIPLIER = 1.25
CACHE_WRITE_1H_MULTIPLIER = 2.0
US_ONLY_MULTIPLIER = 1.1
WEB_SEARCH_USD = 10.00 / 1000


def rate_for(model: str, prompt_tokens: int = 0, fast: bool = False) -> Rate | None:
    if fast and model in FAST_PRICING:
        return FAST_PRICING[model]
    tier = LONG_PROMPT_PRICING.get(model)
    if tier and prompt_tokens > tier[0]:
        return tier[1]
    return MODEL_PRICING.get(model)


def tokens_cost_usd(
    rate: Rate,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    cache_write_1h_tokens: int = 0,
) -> float:
    write_5m = cache_write_tokens - cache_write_1h_tokens
    return (
        input_tokens * rate.input
        + output_tokens * rate.output
        + cache_read_tokens * rate.input * rate.cache_read_multiplier
        + write_5m * rate.input * CACHE_WRITE_5M_MULTIPLIER
        + cache_write_1h_tokens * rate.input * CACHE_WRITE_1H_MULTIPLIER
    ) / 1_000_000


def estimate_cost_usd(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    *,
    cache_write_1h_tokens: int = 0,
    fast: bool = False,
    us_only: bool = False,
    web_searches: int = 0,
) -> float | None:
    """USD for one call; cache_write_tokens includes cache_write_1h_tokens. None if unpriced."""
    rate = rate_for(model, input_tokens + cache_read_tokens + cache_write_tokens, fast)
    if rate is None:
        return None
    tokens = tokens_cost_usd(
        rate,
        input_tokens,
        output_tokens,
        cache_read_tokens,
        cache_write_tokens,
        cache_write_1h_tokens,
    )
    return tokens * (US_ONLY_MULTIPLIER if us_only else 1.0) + web_searches * WEB_SEARCH_USD
