"""Pure computations behind the Claude usage read routes."""

from datetime import UTC, date, datetime

from app.claude_usage import stats


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
