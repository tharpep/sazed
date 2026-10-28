"""Postgres access for automations and automation_runs (migration 003)."""

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from app.automations.models import AutomationCreate, AutomationUpdate, CronSchedule
from app.db import get_pool

_JSON_FIELDS = ("trigger", "action", "outputs", "delivered", "steps")


def _row(rec) -> dict[str, Any]:
    """asyncpg returns JSONB as str (no codec registered); decode it."""
    d = dict(rec)
    for f in _JSON_FIELDS:
        if isinstance(d.get(f), str):
            d[f] = json.loads(d[f])
    return d


def _next_run(trigger: dict, enabled: bool) -> datetime | None:
    if not enabled:
        return None
    return CronSchedule(**trigger).next_after(datetime.now(UTC))


async def list_automations() -> list[dict[str, Any]]:
    rows = await get_pool().fetch(
        """
        SELECT a.*, r.status AS last_status, r.started_at AS last_started_at
        FROM automations a
        LEFT JOIN LATERAL (
            SELECT status, started_at FROM automation_runs
            WHERE automation_id = a.id ORDER BY started_at DESC LIMIT 1
        ) r ON TRUE
        ORDER BY a.enabled DESC, a.next_run_at NULLS LAST, a.name
        """
    )
    return [_row(r) for r in rows]


async def get_automation(automation_id: uuid.UUID) -> dict[str, Any] | None:
    rec = await get_pool().fetchrow("SELECT * FROM automations WHERE id = $1", automation_id)
    return _row(rec) if rec else None


async def create_automation(body: AutomationCreate) -> dict[str, Any]:
    trigger = body.trigger.model_dump()
    rec = await get_pool().fetchrow(
        """
        INSERT INTO automations
            (name, description, enabled, trigger, action, outputs, created_by, next_run_at)
        VALUES ($1, $2, $3, $4::jsonb, $5::jsonb, $6::jsonb, $7, $8)
        RETURNING *
        """,
        body.name,
        body.description,
        body.enabled,
        json.dumps(trigger),
        json.dumps(body.action.model_dump()),
        json.dumps(body.outputs.model_dump()),
        body.created_by,
        _next_run(trigger, body.enabled),
    )
    return _row(rec)


async def update_automation(automation_id: uuid.UUID, body: AutomationUpdate) -> dict | None:
    current = await get_automation(automation_id)
    if current is None:
        return None
    changes = body.model_dump(exclude_unset=True)  # nested models are already plain dicts
    merged = {**current, **changes}
    trigger = merged["trigger"]
    enabled = merged["enabled"]
    # Recompute the next run whenever the schedule or enabled state changes.
    next_run = (
        _next_run(trigger, enabled)
        if "trigger" in changes or "enabled" in changes
        else current["next_run_at"]
    )
    rec = await get_pool().fetchrow(
        """
        UPDATE automations SET
            name = $2, description = $3, enabled = $4, trigger = $5::jsonb,
            action = $6::jsonb, outputs = $7::jsonb, next_run_at = $8, updated_at = NOW()
        WHERE id = $1
        RETURNING *
        """,
        automation_id,
        merged["name"],
        merged["description"],
        enabled,
        json.dumps(trigger),
        json.dumps(merged["action"]),
        json.dumps(merged["outputs"]),
        next_run,
    )
    return _row(rec)


async def delete_automation(automation_id: uuid.UUID) -> bool:
    result = await get_pool().execute("DELETE FROM automations WHERE id = $1", automation_id)
    return result.endswith(" 1")


async def claim_due(now: datetime) -> list[tuple[dict[str, Any], datetime]]:
    """Lock due automations, advance each one's next_run_at, and return (row, scheduled_for).

    Advancing before running means a slow or crashed run can't fire the same slot twice.
    """
    pool = get_pool()
    claimed: list[tuple[dict[str, Any], datetime]] = []
    async with pool.acquire() as conn, conn.transaction():
        rows = await conn.fetch(
            "SELECT * FROM automations WHERE enabled AND next_run_at <= $1 "
            "ORDER BY next_run_at FOR UPDATE SKIP LOCKED",
            now,
        )
        for rec in rows:
            row = _row(rec)
            scheduled_for = row["next_run_at"]
            await conn.execute(
                "UPDATE automations SET next_run_at = $2 WHERE id = $1",
                row["id"],
                _next_run(row["trigger"], True),
            )
            claimed.append((row, scheduled_for))
    return claimed


async def start_run(
    automation_id: uuid.UUID, triggered_by: str, status: str = "running"
) -> uuid.UUID:
    pool = get_pool()
    run_id = await pool.fetchval(
        "INSERT INTO automation_runs (automation_id, triggered_by, status) "
        "VALUES ($1, $2, $3) RETURNING id",
        automation_id,
        triggered_by,
        status,
    )
    await pool.execute("UPDATE automations SET last_run_at = NOW() WHERE id = $1", automation_id)
    return run_id


async def finish_run(
    run_id: uuid.UUID,
    status: str,
    output: str | None = None,
    error: str | None = None,
    session_id: uuid.UUID | None = None,
    delivered: list[str] | None = None,
) -> None:
    await get_pool().execute(
        """
        UPDATE automation_runs SET status = $2, output = $3, error = $4, session_id = $5,
            delivered = $6::jsonb, finished_at = NOW()
        WHERE id = $1
        """,
        run_id,
        status,
        output,
        error,
        session_id,
        json.dumps(delivered or []),
    )


async def set_steps(run_id: uuid.UUID, steps: list[dict[str, Any]]) -> None:
    """An agent run's step record so far (migration 009), rewritten after every step."""
    await get_pool().execute(
        "UPDATE automation_runs SET steps = $2::jsonb WHERE id = $1",
        run_id,
        json.dumps(steps, default=str),
    )


async def automation_for_run(run_id: uuid.UUID) -> dict[str, Any] | None:
    rec = await get_pool().fetchrow(
        "SELECT a.* FROM automations a JOIN automation_runs r ON r.automation_id = a.id "
        "WHERE r.id = $1",
        run_id,
    )
    return _row(rec) if rec else None


async def reopen_run(run_id: uuid.UUID) -> None:
    """A paused run that Pryce approved goes back to running."""
    await get_pool().execute(
        "UPDATE automation_runs SET status = 'running', finished_at = NULL WHERE id = $1", run_id
    )


async def list_runs(automation_id: uuid.UUID, limit: int = 20) -> list[dict[str, Any]]:
    rows = await get_pool().fetch(
        "SELECT * FROM automation_runs WHERE automation_id = $1 ORDER BY started_at DESC LIMIT $2",
        automation_id,
        limit,
    )
    return [_row(r) for r in rows]


async def mark_stale_runs() -> None:
    """Runs left 'running' by a restart can never finish; close them out on startup."""
    await get_pool().execute(
        "UPDATE automation_runs SET status = 'error', error = 'interrupted by restart', "
        "finished_at = NOW() WHERE status = 'running'"
    )
