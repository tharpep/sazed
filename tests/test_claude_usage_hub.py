"""Usage hub: daily rows per source, plan window projections, and the /claude-usage/hub shape."""

from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app import spend_alert
from app.claude_usage import stats, store
from app.config import settings
from app.dependencies import verify_api_key
from app.routers import claude_usage

NOW = datetime(2026, 11, 7, 20, 0, tzinfo=UTC)
DAY = date(2026, 11, 7)
MTOK = 1_000_000


def _tokens(**overrides):
    row = {
        "day": DAY,
        "model": "claude-opus-5-5",
        "long_prompt": False,
        "calls": 1,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
    }
    return {**row, **overrides}


def _rows(**overrides):
    rows = {
        "claude_code": [],
        "side_calls": [],
        "sazed_api": [],
        "jobs": [],
        "fresh": [],
        "readings": [],
    }
    return {**rows, **overrides}


def _reading(minutes_ago, pct, window="five_hour", resets_in=timedelta(hours=2)):
    return {
        "plan_window": window,
        "read_at": NOW - timedelta(minutes=minutes_ago),
        "used_pct": pct,
        "resets_at": NOW + resets_in,
        "status": "allowed",
        "source": "job",
    }


def test_hub_days_labels_each_source_with_its_cost_kind():
    """Claude Code and Claude jobs are API-equivalent, llm_calls billed, agy jobs tokens only."""
    cc = _tokens(machine="desktop-wsl", sidechain=False, output_tokens=MTOK)
    side = {
        "day": DAY,
        "machine": "desktop-wsl",
        "model": "claude-opus-5-5",
        "calls": 2,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "cost_usd": 0.5,
    }
    api = _tokens(model="claude-haiku-5-5", output_tokens=MTOK)
    claude_job = _tokens(agent="claude", output_tokens=MTOK, thinking_tokens=0)
    agy_job = _tokens(
        agent="agy", model="gemini-3.8-flash-high", output_tokens=7, thinking_tokens=3
    )
    days, unpriced = stats.hub_days(
        _rows(claude_code=[cc], side_calls=[side], sazed_api=[api], jobs=[claude_job, agy_job])
    )
    by_source = {d["source"]: d for d in days}
    assert by_source["claude_code"]["usd"] == pytest.approx(20.5)
    assert by_source["claude_code"]["calls"] == 3
    assert by_source["claude_code"]["kind"] == "api_equivalent"
    assert by_source["sazed_api"]["kind"] == "billed"
    assert by_source["sazed_api"]["usd"] == pytest.approx(0.5)
    assert by_source["sazed_api"]["machine"] == "sazed"
    assert by_source["worker_claude"]["usd"] == pytest.approx(20.0)
    assert by_source["worker_agy"]["usd"] is None
    assert by_source["worker_agy"]["kind"] == "tokens"
    assert by_source["worker_agy"]["thinking_tokens"] == 3
    assert by_source["worker_agy"]["machine"] == "homelab"
    assert unpriced == {}


def test_hub_days_counts_unpriced_models_instead_of_dropping_their_tokens():
    days, unpriced = stats.hub_days(_rows(sazed_api=[_tokens(model="mystery", calls=4)]))
    assert days[0]["usd"] is None
    assert unpriced == {"mystery": 4}


def test_projection_extends_the_pace_of_readings_in_the_current_window():
    """60 to 80 percent over an hour, two hours before reset, projects past 100 and caps."""
    assert stats._projection([_reading(60, 60.0), _reading(0, 80.0)], NOW) == 100.0
    soon = timedelta(minutes=30)
    slow = [_reading(60, 60.0, resets_in=soon), _reading(0, 62.0, resets_in=soon)]
    assert stats._projection(slow, NOW) == 63.0


def test_projection_is_none_without_enough_span_or_after_reset():
    assert stats._projection([_reading(5, 50.0), _reading(0, 55.0)], NOW) is None
    past = -timedelta(minutes=1)
    reset = [_reading(90, 10.0, resets_in=past), _reading(0, 20.0, resets_in=past)]
    assert stats._projection(reset, NOW) is None


def test_projection_ignores_readings_from_an_earlier_window():
    old = _reading(300, 95.0, resets_in=-timedelta(hours=1))
    assert stats._projection([old, _reading(0, 10.0)], NOW) is None


def test_hub_limits_reports_the_latest_reading_per_window():
    readings = [
        _reading(60, 40.0),
        _reading(0, 84.0),
        _reading(0, 41.0, window="seven_day", resets_in=-timedelta(minutes=5)),
    ]
    limits = {item["window"]: item for item in stats.hub_limits(readings, NOW)}
    assert limits["five_hour"]["used_pct"] == 84.0
    assert limits["five_hour"]["reset_since"] is False
    assert len(limits["five_hour"]["points"]) == 2
    assert limits["seven_day"]["reset_since"] is True
    assert limits["seven_day"]["projected_pct"] is None


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "api_key", "console-key")
    app = FastAPI()
    app.include_router(
        claude_usage.router, prefix="/claude-usage", dependencies=[Depends(verify_api_key)]
    )
    return TestClient(app, headers={"X-API-Key": "console-key"})


def _spend(total):
    async def fake(start, end):
        return {"total": total, "by_model": {}, "by_purpose": {}, "unpriced": []}

    return fake


def test_hub_route_shape(client, monkeypatch):
    seen = {}

    async def fake_rows(frm, to, tz):
        seen["tz"] = tz
        return _rows(
            jobs=[_tokens(agent="claude", output_tokens=MTOK, thinking_tokens=0)],
            fresh=[{"source": "worker_claude", "machine": None, "last_at": NOW}],
            readings=[_reading(0, 84.0)],
        )

    monkeypatch.setattr(store, "hub_rows", fake_rows)
    monkeypatch.setattr(spend_alert, "month_spend", _spend(0.591234))
    monkeypatch.setattr(settings, "spend_alert_timezone", "America/New_York")
    monkeypatch.setattr(settings, "spend_alert_monthly_usd", "25")
    body = client.get("/claude-usage/hub?days=7").json()
    assert seen["tz"] == "America/New_York"
    assert body["timezone"] == "America/New_York"
    assert body["billed"]["usd"] == 0.5912
    assert body["billed"]["alert_usd"] == 25.0
    assert body["days"][0]["source"] == "worker_claude"
    assert body["sources"] == [
        {"source": "worker_claude", "machine": "homelab", "last_at": "2026-11-07T20:00:00Z"}
    ]
    assert body["limits"][0]["window"] == "five_hour"
    assert set(body) == {
        "range",
        "timezone",
        "prices_as_of",
        "limits",
        "billed",
        "days",
        "sources",
        "unpriced",
    }


def test_hub_alert_is_null_when_the_limit_is_off(client, monkeypatch):
    async def fake_rows(frm, to, tz):
        return _rows()

    monkeypatch.setattr(store, "hub_rows", fake_rows)
    monkeypatch.setattr(spend_alert, "month_spend", _spend(0.0))
    monkeypatch.setattr(settings, "spend_alert_monthly_usd", "")
    body = client.get("/claude-usage/hub").json()
    assert body["billed"]["alert_usd"] is None
    assert body["days"] == []
    assert body["limits"] == []
