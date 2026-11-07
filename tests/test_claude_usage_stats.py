"""Pure computations behind the Claude usage read routes."""

from datetime import UTC, date, datetime

import pytest

from app.claude_usage import stats
from app.claude_usage.stats import priced


def _t(minute: int) -> datetime:
    return datetime(2026, 11, 3, 1, minute, tzinfo=UTC)


def test_median_empty_is_none():
    assert stats.median([]) is None


def test_median_odd_even_and_unsorted():
    assert stats.median([5]) == 5
    assert stats.median([9, 1, 5]) == 5
    assert stats.median([1, 2, 3, 10]) == 2.5


def test_rate_is_none_for_zero_denominator():
    assert stats.rate(0, 0) is None
    assert stats.rate(5, 0) is None


def test_rate_rounds_and_keeps_zero():
    assert stats.rate(1, 3) == 0.333
    assert stats.rate(0, 4) == 0.0
    assert stats.rate(4, 4) == 1.0


def test_iso_week_pads_and_uses_iso_year():
    assert stats.iso_week(date(2026, 11, 2)) == "2026-W45"
    assert stats.iso_week(date(2026, 1, 1)) == "2026-W01"
    assert stats.iso_week(date(2027, 1, 1)) == "2026-W53"
    assert stats.iso_week(datetime(2026, 10, 26, 23, 59, tzinfo=UTC)) == "2026-W44"


def test_context_size_sums_input_and_cache():
    assert stats.context_size(10, 100, 5) == 115


def test_sample_series_empty_and_short_returned_whole():
    assert stats.sample_series([]) == []
    short = [{"ts": str(i), "context": i} for i in range(59)]
    assert stats.sample_series(short) == short
    exact = [{"ts": str(i), "context": i} for i in range(60)]
    assert stats.sample_series(exact) == exact


def test_sample_series_caps_at_limit_in_order_with_ends():
    points = [{"ts": f"{i:04d}", "context": i} for i in range(1000)]
    out = stats.sample_series(points)
    assert len(out) == 60
    assert out == sorted(out, key=lambda p: p["ts"])
    assert out[0] == points[0]
    assert out[-1] == points[-1]


def test_sample_series_always_keeps_a_mid_series_peak():
    points = [{"ts": f"{i:04d}", "context": 1} for i in range(1000)]
    points[501]["context"] = 999
    out = stats.sample_series(points)
    assert len(out) == 60
    assert points[501] in out
    assert out == sorted(out, key=lambda p: p["ts"])


def test_context_before_picks_latest_strictly_earlier_or_zero():
    series = [(_t(1), 10), (_t(5), 50), (_t(9), 90)]
    assert stats.context_before([], _t(5)) == 0
    assert stats.context_before(series, _t(1)) == 0
    assert stats.context_before(series, _t(5)) == 10
    assert stats.context_before(series, _t(6)) == 50
    assert stats.context_before(series, _t(59)) == 90


def test_main_model_most_common_with_stable_tie_and_none():
    assert stats.main_model([]) is None
    assert stats.main_model([None, None]) is None
    assert stats.main_model(["b", "a", "b", None]) == "b"
    assert stats.main_model(["b", "a"]) == "a"


def _group(**overrides):
    row = {
        "model": "claude-opus-5-5",
        "long_prompt": False,
        "fast": False,
        "us_only": False,
        "web_searches": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "cache_write_1h_tokens": 0,
    }
    return {**row, **overrides}


def test_priced_applies_every_modifier_a_group_carries():
    """A group's flags select fast, long-prompt, region and web-search pricing."""
    mtok = 1_000_000
    assert priced(_group(output_tokens=mtok, fast=True))[0] == pytest.approx(40.0)
    long_haiku = _group(model="claude-haiku-5-5", output_tokens=mtok, long_prompt=True)
    assert priced(long_haiku)[0] == pytest.approx(2.5)
    us = _group(output_tokens=mtok, us_only=True, web_searches=10)
    assert priced(us)[0] == pytest.approx(22.0 + 0.1)
    one_hour = _group(cache_write_tokens=mtok, cache_write_1h_tokens=mtok)
    assert priced(one_hour) == pytest.approx((8.0, 4.0))
    assert priced(_group(model=None)) is None
