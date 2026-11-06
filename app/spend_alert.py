"""Monthly API spend alert: Pushover when month-to-date cost passes a threshold.

Every CHECK_SECONDS this sums the calendar month's llm_calls (month in
settings.spend_alert_timezone), prices them with estimate_cost_usd, and sends one Pushover per
threshold step per month through the gateway. spend_alerts remembers what was sent, so a check
never repeats within the month and a new month starts clean. Worker jobs aren't per-token and
aren't in llm_calls, so they're out of scope. Alert only; nothing is blocked.
Runs only where the worker is configured (WORKER_KEY set), like idle-session processing.
"""

import asyncio
import logging
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.config import settings
from app.db import get_pool
from app.jobs.service import notify_simple
from app.pricing import estimate_cost_usd

logger = logging.getLogger(__name__)

CHECK_SECONDS = 3600
TOP_N = 3
_task: asyncio.Task | None = None


def monthly_limit() -> float:
    """The configured monthly limit in USD; 0 when empty, zero or not a number (= off)."""
    raw = (settings.spend_alert_monthly_usd or "").strip().lstrip("$")
    try:
        return max(float(raw), 0.0) if raw else 0.0
    except ValueError:
        logger.warning("SPEND_ALERT_MONTHLY_USD %r is not a number; alerts off", raw)
        return 0.0


def _timezone() -> ZoneInfo:
    try:
        return ZoneInfo(settings.spend_alert_timezone)
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning("Unknown spend alert timezone %r; using UTC", settings.spend_alert_timezone)
        return ZoneInfo("UTC")


def month_bounds(now: datetime, tz: ZoneInfo) -> tuple[datetime, datetime, str]:
    """(start, end, 'YYYY-MM') of the calendar month containing `now`, as aware datetimes."""
    local = now.astimezone(tz)
    start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = (start.replace(year=start.year + 1, month=1) if start.month == 12
           else start.replace(month=start.month + 1))
    return start, end, start.strftime("%Y-%m")


async def month_spend(start: datetime, end: datetime) -> dict:
    """Month-to-date cost with per-model and per-purpose breakdowns.

    `unpriced` lists models in llm_calls that estimate_cost_usd has no price for; their calls
    add nothing to the total, so the alert says so instead of undercounting silently.
    """
    rows = await get_pool().fetch(
        """
        SELECT model, COALESCE(purpose, 'unknown') AS purpose, COUNT(*) AS calls,
               COALESCE(SUM(input_tokens), 0) AS input_tokens,
               COALESCE(SUM(output_tokens), 0) AS output_tokens,
               COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
               COALESCE(SUM(cache_write_tokens), 0) AS cache_write_tokens
        FROM llm_calls
        WHERE timestamp >= $1 AND timestamp < $2
        GROUP BY model, COALESCE(purpose, 'unknown')
        """,
        start,
        end,
    )
    total = 0.0
    by_model: dict[str, float] = {}
    by_purpose: dict[str, float] = {}
    unpriced: set[str] = set()
    for r in rows:
        cost = estimate_cost_usd(
            r["model"],
            r["input_tokens"],
            r["output_tokens"],
            r["cache_read_tokens"],
            r["cache_write_tokens"],
        )
        if cost is None:
            unpriced.add(r["model"])
            continue
        total += cost
        by_model[r["model"]] = by_model.get(r["model"], 0.0) + cost
        by_purpose[r["purpose"]] = by_purpose.get(r["purpose"], 0.0) + cost
    return {"total": total, "by_model": by_model, "by_purpose": by_purpose,
            "unpriced": sorted(unpriced)}


def _top(costs: dict[str, float]) -> str:
    ranked = sorted(costs.items(), key=lambda kv: kv[1], reverse=True)[:TOP_N]
    return ", ".join(f"{name} ${cost:.2f}" for name, cost in ranked) or "none"


def format_alert(
    spend: dict, limit: float, pct: int, now: datetime, start: datetime, end: datetime
) -> tuple[str, str]:
    """(title, body) for the Pushover."""
    total = spend["total"]
    elapsed = (now - start) / (end - start)
    projected = total / elapsed if elapsed > 0 else total
    title = f"API spend at ${total:.2f} ({pct}% of ${limit:.0f})"
    lines = [
        f"Month to date ${total:.2f} of ${limit:.2f}; projected ${projected:.2f} by month end.",
        f"Top purposes: {_top(spend['by_purpose'])}",
        f"Top models: {_top(spend['by_model'])}",
    ]
    if spend["unpriced"]:
        lines.append(
            "Not counted, no price for: " + ", ".join(spend["unpriced"]) + ". Real spend is higher."
        )
    return title, "\n".join(lines)


async def _claim(month: str, pcts: list[int], total: float) -> list[int]:
    """Record the steps as sent; returns the ones this call newly recorded (none = already sent)."""
    claimed = []
    for pct in pcts:
        row = await get_pool().fetchrow(
            "INSERT INTO spend_alerts (month, pct, spend_usd) VALUES ($1, $2, $3) "
            "ON CONFLICT (month, pct) DO NOTHING RETURNING pct",
            month,
            pct,
            total,
        )
        if row:
            claimed.append(pct)
    return claimed


async def check_spend(now: datetime | None = None) -> bool:
    """One check. Returns True if an alert was sent."""
    limit = monthly_limit()
    if limit <= 0:
        return False
    now = now or datetime.now(UTC)
    start, end, month = month_bounds(now, _timezone())
    spend = await month_spend(start, end)
    crossed = sorted(p for p in set(settings.spend_alert_steps_pct)
                     if p > 0 and spend["total"] >= limit * p / 100)
    if not crossed:
        return False
    # Claim before sending so overlapping checks can't double-send. One message covers every
    # step crossed at once (a jump from under 75% to over 100%); the highest is its headline.
    claimed = await _claim(month, crossed, spend["total"])
    if not claimed:
        return False
    title, body = format_alert(spend, limit, max(claimed), now, start, end)
    await notify_simple(title, body, f"{settings.console_url}/?page=audit", "Cost view")
    logger.info("Spend alert sent: %s", title)
    return True


async def _loop() -> None:
    while True:
        try:
            await check_spend()
        except Exception:
            logger.exception("Spend alert check failed")
        await asyncio.sleep(CHECK_SECONDS)


async def start() -> None:
    global _task
    if monthly_limit() <= 0 or not settings.worker_key or not settings.database_url:
        logger.info("Spend alert disabled (no limit set, or not the homelab)")
        return
    _task = asyncio.create_task(_loop())
    logger.info("Spend alert started (every %ss, limit $%.2f)", CHECK_SECONDS, monthly_limit())


async def stop() -> None:
    if _task is not None:
        _task.cancel()
