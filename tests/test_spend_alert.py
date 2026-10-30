"""Monthly spend alert: one Pushover per step per month, unpriced models called out, month reset."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from app import spend_alert
from app.config import settings

MTOK = 1_000_000
TZ = ZoneInfo("America/Indiana/Indianapolis")


class _FakePool:
    def __init__(self, rows):
        self.rows = rows
        self.sent: set[tuple[str, int]] = set()

    async def fetch(self, query, *args):
        return self.rows

    async def fetchrow(self, query, month, pct, total):
        if (month, pct) in self.sent:
            return None
        self.sent.add((month, pct))
        return {"pct": pct}


def _row(model, purpose, out_tokens):
    return {
        "model": model, "purpose": purpose, "calls": 1, "input_tokens": 0,
        "output_tokens": out_tokens, "cache_read_tokens": 0, "cache_write_tokens": 0,
    }


@pytest.fixture
def env(monkeypatch):
    pool = _FakePool([])
    messages = []

    async def fake_notify(title, body, url=None, url_title=None):
        messages.append((title, body, url))

    monkeypatch.setattr(spend_alert, "get_pool", lambda: pool)
    monkeypatch.setattr(spend_alert, "notify_simple", fake_notify)
    monkeypatch.setattr(settings, "spend_alert_monthly_usd", "10")
    monkeypatch.setattr(settings, "spend_alert_steps_pct", [100])
    monkeypatch.setattr(settings, "spend_alert_timezone", "America/Indiana/Indianapolis")
    return pool, messages


NOW = datetime(2026, 10, 15, 12, tzinfo=UTC)


@pytest.mark.parametrize(
    "raw,expected", [("", 0), ("0", 0), ("abc", 0), ("25", 25), ("$12.5", 12.5)]
)
def test_monthly_limit(monkeypatch, raw, expected):
    monkeypatch.setattr(settings, "spend_alert_monthly_usd", raw)
    assert spend_alert.monthly_limit() == expected


def test_month_bounds_use_local_zone():
    # 02:00 UTC on Nov 1 is still Oct 31 in Indianapolis.
    start, end, month = spend_alert.month_bounds(datetime(2026, 11, 1, 2, tzinfo=UTC), TZ)
    assert month == "2026-10"
    assert start == datetime(2026, 10, 1, tzinfo=TZ)
    assert end == datetime(2026, 11, 1, tzinfo=TZ)
    assert spend_alert.month_bounds(datetime(2026, 12, 20, tzinfo=UTC), TZ)[1].year == 2027


async def test_off_when_unset(env, monkeypatch):
    monkeypatch.setattr(settings, "spend_alert_monthly_usd", "")
    assert await spend_alert.check_spend(NOW) is False
    assert env[1] == []


async def test_below_threshold_sends_nothing(env):
    pool, messages = env
    pool.rows = [_row("claude-sonnet-5-5", "chat", MTOK // 2)]  # $5
    assert await spend_alert.check_spend(NOW) is False
    assert messages == []


async def test_alert_sent_once_per_month(env):
    pool, messages = env
    pool.rows = [
        _row("claude-sonnet-5-5", "chat", MTOK),  # $10
        _row("claude-opus-5-5", "think", MTOK // 2),  # $10
    ]
    assert await spend_alert.check_spend(NOW) is True
    assert await spend_alert.check_spend(NOW) is False
    assert len(messages) == 1
    title, body, url = messages[0]
    assert "$20.00" in title
    assert "projected" in body and "think $10.00" in body and "claude-opus-5-5 $10.00" in body
    assert url.endswith("/?page=audit")


async def test_new_month_alerts_again(env):
    pool, messages = env
    pool.rows = [_row("claude-sonnet-5-5", "chat", MTOK)]
    assert await spend_alert.check_spend(NOW) is True
    assert await spend_alert.check_spend(datetime(2026, 11, 15, tzinfo=UTC)) is True
    assert len(messages) == 2


async def test_steps_fire_separately_and_jump_sends_one(env, monkeypatch):
    pool, messages = env
    monkeypatch.setattr(settings, "spend_alert_steps_pct", [75, 100])
    pool.rows = [_row("claude-sonnet-5-5", "chat", 800_000)]  # $8 = 80%
    assert await spend_alert.check_spend(NOW) is True
    pool.rows = [_row("claude-sonnet-5-5", "chat", MTOK)]  # $10 = 100%
    assert await spend_alert.check_spend(NOW) is True
    assert await spend_alert.check_spend(NOW) is False
    assert "80%" not in messages[0][0] and "75%" in messages[0][0]
    assert "100%" in messages[1][0]

    pool.sent.clear()
    messages.clear()
    assert await spend_alert.check_spend(NOW) is True  # both steps crossed at once: one message
    assert len(messages) == 1 and "100%" in messages[0][0]


async def test_unpriced_model_is_called_out(env):
    pool, messages = env
    pool.rows = [
        _row("claude-sonnet-5-5", "chat", MTOK),
        _row("claude-mystery-9", "chat", MTOK),
    ]
    assert await spend_alert.check_spend(NOW) is True
    assert "claude-mystery-9" in messages[0][1]
    assert "higher" in messages[0][1]
