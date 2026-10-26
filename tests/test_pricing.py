import pytest

from app.pricing import estimate_cost_usd

MTOK = 1_000_000


def test_unpriced_model_returns_none():
    assert estimate_cost_usd("claude-unknown", 100, 100) is None


@pytest.mark.parametrize(
    ("model", "input_rate", "output_rate"),
    [
        ("claude-haiku-5-5", 0.10, 0.50),
        ("claude-sonnet-5-5", 2.00, 10.00),
        ("claude-opus-5-5", 4.00, 20.00),
    ],
)
def test_input_and_output_rates(model, input_rate, output_rate):
    assert estimate_cost_usd(model, MTOK, 0) == pytest.approx(input_rate)
    assert estimate_cost_usd(model, 0, MTOK) == pytest.approx(output_rate)


def test_cache_reads_default_to_a_tenth_of_input():
    assert estimate_cost_usd("claude-sonnet-5-5", 0, 0, cache_read_tokens=MTOK) == pytest.approx(
        0.20
    )


def test_opus_cache_reads_use_their_own_rate():
    # $0.20/Mtok, 0.05x Opus 5.5's input rate rather than the default 0.1x.
    assert estimate_cost_usd("claude-opus-5-5", 0, 0, cache_read_tokens=MTOK) == pytest.approx(0.20)


def test_cache_writes_cost_more_than_input():
    assert estimate_cost_usd("claude-opus-5-5", 0, 0, cache_write_tokens=MTOK) == pytest.approx(
        5.00
    )
