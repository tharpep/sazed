import pytest

from app.pricing import estimate_cost_usd

MTOK = 1_000_000
SMALL = 50_000


def test_unpriced_model_returns_none():
    assert estimate_cost_usd("claude-unknown", 100, 100) is None


@pytest.mark.parametrize(
    ("model", "input_rate", "output_rate"),
    [
        ("claude-haiku-5-5", 0.10, 0.50),
        ("claude-sonnet-5-5", 2.00, 10.00),
        ("claude-opus-5-5", 4.00, 20.00),
        ("claude-fable-5-1", 10.00, 50.00),
    ],
)
def test_input_and_output_rates(model, input_rate, output_rate):
    assert estimate_cost_usd(model, SMALL, 0) == pytest.approx(input_rate * SMALL / MTOK)
    assert estimate_cost_usd(model, 0, MTOK) == pytest.approx(output_rate)


@pytest.mark.parametrize(
    ("model", "read_rate"),
    [
        ("claude-opus-5-5", 0.20),
        ("claude-sonnet-5-5", 0.10),
        ("claude-fable-5-1", 0.25),
        ("claude-opus-4-8", 0.50),
    ],
)
def test_cache_read_rates_match_the_price_list(model, read_rate):
    assert estimate_cost_usd(model, 0, 0, cache_read_tokens=MTOK) == pytest.approx(read_rate)


def test_cache_writes_split_into_five_minute_and_one_hour():
    """A 1h write costs 2x input and a 5m write 1.25x, from one combined total."""
    cost = estimate_cost_usd(
        "claude-opus-5-5", 0, 0, cache_write_tokens=2 * MTOK, cache_write_1h_tokens=MTOK
    )
    assert cost == pytest.approx(5.00 + 8.00)


def test_haiku_prompts_over_100k_use_the_long_prompt_rate():
    """Prompt length counts cache reads and writes, and the higher rate covers the whole call."""
    cost = estimate_cost_usd("claude-haiku-5-5", 1_000, MTOK, cache_read_tokens=100_000)
    assert cost == pytest.approx((1_000 * 0.50 + MTOK * 2.50 + 100_000 * 0.05) / MTOK)


def test_fast_mode_uses_premium_rates_only_where_offered():
    assert estimate_cost_usd("claude-opus-5-5", 0, MTOK, fast=True) == pytest.approx(40.00)
    assert estimate_cost_usd("claude-sonnet-5-5", 0, MTOK, fast=True) == pytest.approx(10.00)


def test_us_only_inference_and_web_searches_add_on():
    cost = estimate_cost_usd("claude-sonnet-5-5", 0, MTOK, us_only=True, web_searches=100)
    assert cost == pytest.approx(10.00 * 1.1 + 1.00)
