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
        "plans": [],
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


def _projected(readings):
    return stats._pace(readings, NOW)["projected_pct"]


def test_projection_extends_the_pace_of_readings_in_the_current_window():
    """60 to 80 percent over an hour, two hours before reset, projects past 100 and caps."""
    assert _projected([_reading(60, 60.0), _reading(0, 80.0)]) == 100.0
    soon = timedelta(minutes=30)
    slow = [_reading(60, 60.0, resets_in=soon), _reading(0, 62.0, resets_in=soon)]
    assert _projected(slow) == 63.0


def test_projection_is_none_without_enough_span_or_after_reset():
    assert _projected([_reading(5, 50.0), _reading(0, 55.0)]) is None
    past = -timedelta(minutes=1)
    reset = [_reading(90, 10.0, resets_in=past), _reading(0, 20.0, resets_in=past)]
    assert _projected(reset) is None


def test_projection_ignores_readings_from_an_earlier_window():
    old = _reading(300, 95.0, resets_in=-timedelta(hours=1))
    assert _projected([old, _reading(0, 10.0)]) is None


def test_pace_uses_the_last_hour_not_the_whole_window():
    """A burst in the last hour sets the rate even when the window started slowly."""
    later = timedelta(hours=3)
    readings = [_reading(240, 0.0, resets_in=later), _reading(60, 10.0, resets_in=later)]
    pace = stats._pace([*readings, _reading(0, 40.0, resets_in=later)], NOW)
    assert pace["full_at"] == "2026-11-07T22:00:00Z"
    assert pace["projected_pct"] == 100.0


def test_pace_falls_back_to_the_whole_window_when_the_last_hour_is_thin():
    readings = [_reading(120, 20.0), _reading(3, 50.0), _reading(0, 50.0)]
    assert stats._pace(readings, NOW)["projected_pct"] == 80.0


def test_pace_has_no_full_time_when_the_limit_lands_after_reset():
    readings = [_reading(60, 10.0), _reading(0, 20.0)]
    pace = stats._pace(readings, NOW)
    assert pace["full_at"] is None
    assert pace["projected_pct"] == 40.0


def test_even_pace_is_the_share_of_the_window_elapsed():
    pace = stats._pace([_reading(0, 50.0)], NOW)
    assert pace["even_pct"] == 60.0
    assert pace["allowance_per_day"] is None


def test_weekly_allowance_spreads_what_is_left_over_the_days_to_reset():
    week = [_reading(0, 91.0, window="seven_day", resets_in=timedelta(days=2, hours=12))]
    pace = stats._pace(week, NOW)
    assert pace["allowance_per_day"] == 3.6
    assert pace["even_pct"] == 64.3


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
        "value",
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


def _day(kind, cost):
    return {"kind": kind, "usd": cost}


def test_plan_value_prices_each_day_by_the_period_in_force():
    """A mid-range plan change prices days before and after it at their own monthly rate."""
    periods = [
        {"starts_on": date(2026, 10, 1), "plan": "Pro", "monthly_usd": 31.0},
        {"starts_on": date(2026, 10, 11), "plan": "Max", "monthly_usd": 310.0},
    ]
    days = [_day("api_equivalent", 500.0), _day("billed", 3.0), _day("tokens", None)]
    value = stats.plan_value(periods, days, date(2026, 10, 6), date(2026, 10, 15))
    assert value["plan_usd"] == 5 * 1.0 + 5 * 10.0
    assert value["api_equivalent_usd"] == 500.0
    assert value["multiple"] == 9.1
    assert value["plan"] == "Max"
    assert value["since"] == "2026-10-06"


def test_plan_value_starts_at_the_first_period_and_is_none_without_one():
    periods = [{"starts_on": date(2026, 11, 6), "plan": "Max", "monthly_usd": 300.0}]
    value = stats.plan_value(periods, [], date(2026, 11, 1), date(2026, 11, 7))
    assert value["plan_usd"] == 20.0
    assert value["since"] == "2026-11-06"
    assert stats.plan_value([], [], date(2026, 11, 1), date(2026, 11, 7)) is None


def _state(read_ago=None, resets_in=None, active_ago=None):
    return {
        "last_read": None if read_ago is None else NOW - read_ago,
        "five_hour_resets": None if resets_in is None else NOW + resets_in,
        "last_active": None if active_ago is None else NOW - active_ago,
    }


def test_probe_waits_while_the_last_reading_is_fresh():
    state = _state(read_ago=timedelta(minutes=4), resets_in=timedelta(hours=1))
    assert stats.probe_due(state, NOW) == {"due": False, "reason": "fresh"}


def test_probe_reads_while_the_five_hour_window_is_open():
    state = _state(read_ago=timedelta(minutes=30), resets_in=timedelta(hours=1))
    assert stats.probe_due(state, NOW)["reason"] == "window_open"


def test_probe_never_opens_a_window_without_activity():
    """After a reset the probe stays idle until a machine reports Claude in use."""
    closed = _state(read_ago=timedelta(hours=6), resets_in=-timedelta(hours=1))
    assert stats.probe_due(closed, NOW) == {"due": False, "reason": "idle"}
    active = {**closed, "last_active": NOW - timedelta(minutes=2)}
    assert stats.probe_due(active, NOW) == {"due": True, "reason": "active"}
    assert stats.probe_due(_state(), NOW) == {"due": False, "reason": "idle"}


def test_dispatch_readings_route_posts_both_windows(client, monkeypatch):
    seen = {}

    async def fake_add(info, read_at, source, machine):
        seen.update(info=info, source=source, machine=machine, read_at=read_at)
        return 2

    monkeypatch.setattr(store, "add_reading", fake_add)
    info = {"unifiedWindows": {"five_hour": {"utilization": 0.5, "resetsAt": 1794103800}}}
    body = {"machine": "PryceSurface-wsl", "rate_limit_info": info}
    assert client.post("/claude-usage/readings", json=body).json() == {"added": 2}
    assert seen["source"] == "dispatch"
    assert seen["read_at"].tzinfo is not None
    assert client.post("/claude-usage/readings", json={"machine": "a b"}).status_code == 422


def test_plans_route_saves_a_period(client, monkeypatch):
    saved = []

    async def fake_set(starts_on, plan, monthly_usd):
        saved.append((starts_on, plan, monthly_usd))

    async def fake_list():
        return [{"starts_on": date(2026, 11, 1), "plan": "Max", "monthly_usd": 100.0}]

    monkeypatch.setattr(store, "set_plan_period", fake_set)
    monkeypatch.setattr(store, "plan_periods", fake_list)
    body = {"starts_on": "2026-11-01", "plan": "Max", "monthly_usd": 100}
    res = client.post("/claude-usage/plans", json=body).json()
    assert saved == [(date(2026, 11, 1), "Max", 100.0)]
    assert res["periods"][0]["plan"] == "Max"


def test_activity_needs_the_usage_key(monkeypatch):
    monkeypatch.setattr(settings, "claude_usage_key", "push-key")
    touched = []

    async def fake_touch(machine, at):
        touched.append(machine)

    monkeypatch.setattr(store, "touch_activity", fake_touch)
    app = FastAPI()
    app.include_router(claude_usage.ingest_router, prefix="/claude-usage")
    c = TestClient(app)
    assert c.post("/claude-usage/activity", json={"machine": "PryceSurface"}).status_code == 401
    ok = c.post(
        "/claude-usage/activity",
        json={"machine": "PryceSurface"},
        headers={"X-Claude-Usage-Key": "push-key"},
    )
    assert ok.status_code == 200
    assert touched == ["PryceSurface"]


def test_worker_limit_routes_record_probe_readings(monkeypatch):
    from app.routers import worker

    monkeypatch.setattr(settings, "worker_key", "w-key")
    seen = {}

    async def fake_add(info, read_at, source, machine):
        seen.update(source=source, machine=machine)
        return 2

    async def fake_state():
        return {"last_read": None, "five_hour_resets": None, "last_active": None}

    monkeypatch.setattr(store, "add_reading", fake_add)
    monkeypatch.setattr(store, "probe_state", fake_state)
    app = FastAPI()
    app.include_router(worker.router, prefix="/worker")
    c = TestClient(app, headers={"X-Worker-Key": "w-key"})
    assert c.post("/worker/limits/due").json() == {"due": False, "reason": "idle"}
    res = c.post("/worker/limits/reading", json={"rate_limit_info": {"unifiedWindows": {}}})
    assert res.json() == {"added": 2}
    assert seen == {"source": "probe", "machine": "homelab"}
