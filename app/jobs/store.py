"""Postgres access for jobs and job_events (migration 004)."""

import json
import uuid
from typing import Any

from app.db import get_pool

_JSON_FIELDS = ("params", "meta", "event")
FINAL_STATUSES = ("succeeded", "failed", "cancelled", "timed_out")

# A running job whose worker hasn't reported in this long is presumed dead.
_HEARTBEAT_GRACE_S = 120


def _row(rec) -> dict[str, Any]:
    d = dict(rec)
    for f in _JSON_FIELDS:
        if isinstance(d.get(f), str):
            d[f] = json.loads(d[f])
    return d


async def create_job(
    *,
    kind: str,
    agent: str,
    model: str,
    profile: str,
    title: str,
    prompt: str,
    params: dict[str, Any],
    requested_by: str,
    status: str = "queued",
    parent_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    rec = await get_pool().fetchrow(
        """
        INSERT INTO jobs (kind, agent, model, profile, title, prompt, params, status,
                          parent_id, requested_by)
        VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9, $10)
        RETURNING *
        """,
        kind,
        agent,
        model,
        profile,
        title,
        prompt,
        json.dumps(params),
        status,
        parent_id,
        requested_by,
    )
    return _row(rec)


async def get_job(job_id: uuid.UUID) -> dict[str, Any] | None:
    rec = await get_pool().fetchrow("SELECT * FROM jobs WHERE id = $1", job_id)
    return _row(rec) if rec else None


async def list_jobs(status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    rows = await get_pool().fetch(
        """
        SELECT id, kind, agent, model, profile, title, status, requested_by, parent_id,
               created_at, started_at, finished_at, left(result, 300) AS result_preview, error
        FROM jobs
        WHERE ($1::text IS NULL OR status = $1)
        ORDER BY created_at DESC
        LIMIT $2
        """,
        status,
        limit,
    )
    return [_row(r) for r in rows]


async def get_events(job_id: uuid.UUID, limit: int = 500) -> list[dict[str, Any]]:
    rows = await get_pool().fetch(
        "SELECT at, event FROM job_events WHERE job_id = $1 ORDER BY id LIMIT $2",
        job_id,
        limit,
    )
    return [_row(r) for r in rows]


async def count_recent(agent: str) -> int:
    """Capped jobs (automations, think, CI fix-ups, Sazed alone) for this agent in the last 24h."""
    return await get_pool().fetchval(
        """
        SELECT COUNT(*) FROM jobs
        WHERE agent = $1 AND created_at > NOW() - INTERVAL '24 hours'
          AND status NOT IN ('cancelled', 'needs_approval')
          AND (requested_by LIKE 'automation%' OR requested_by LIKE 'think:%'
               OR requested_by LIKE 'ci-fix%' OR requested_by = 'sazed')
        """,
        agent,
    )


async def expire_stale() -> list[dict[str, Any]]:
    """Mark running jobs whose worker went silent (or overran their timeout) as timed_out."""
    rows = await get_pool().fetch(
        f"""
        UPDATE jobs SET status = 'timed_out', finished_at = NOW(),
               error = COALESCE(error, 'worker stopped reporting')
        WHERE status = 'running'
          AND (heartbeat_at < NOW() - INTERVAL '{_HEARTBEAT_GRACE_S} seconds'
               OR started_at + make_interval(
                    secs => COALESCE((params->>'timeout_s')::int, 300) + {_HEARTBEAT_GRACE_S}
                  ) < NOW())
        RETURNING *
        """
    )
    return [_row(r) for r in rows]


async def claim_next(agents: list[str]) -> dict[str, Any] | None:
    """Hand the oldest queued job for one of these agents to the worker (one claimant wins)."""
    rec = await get_pool().fetchrow(
        """
        UPDATE jobs SET status = 'running', started_at = NOW(), heartbeat_at = NOW()
        WHERE id = (
            SELECT id FROM jobs
            WHERE status = 'queued' AND agent = ANY($1::text[])
            ORDER BY created_at
            FOR UPDATE SKIP LOCKED
            LIMIT 1
        )
        RETURNING *
        """,
        agents,
    )
    return _row(rec) if rec else None


async def add_events(job_id: uuid.UUID, events: list[dict[str, Any]]) -> str | None:
    """Store streamed events and refresh the heartbeat. Returns the job's current status."""
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            status = await conn.fetchval(
                "UPDATE jobs SET heartbeat_at = NOW() WHERE id = $1 RETURNING status", job_id
            )
            if status is None:
                return None
            if events:
                await conn.executemany(
                    "INSERT INTO job_events (job_id, event) VALUES ($1, $2::jsonb)",
                    [(job_id, json.dumps(e)) for e in events],
                )
    return status


async def finish_job(
    job_id: uuid.UUID,
    status: str,
    result: str | None,
    error: str | None,
    agent_session_id: str | None,
    meta: dict[str, Any],
) -> dict[str, Any] | None:
    """Record the outcome, unless the job was already finalized (cancelled or timed out)."""
    rec = await get_pool().fetchrow(
        """
        UPDATE jobs SET status = $2, result = $3, error = $4, agent_session_id = $5,
               meta = $6::jsonb, finished_at = NOW()
        WHERE id = $1 AND status = 'running'
        RETURNING *
        """,
        job_id,
        status,
        result,
        error,
        agent_session_id,
        json.dumps(meta),
    )
    return _row(rec) if rec else None


async def cancel_job(job_id: uuid.UUID) -> dict[str, Any] | None:
    rec = await get_pool().fetchrow(
        """
        UPDATE jobs SET status = 'cancelled', finished_at = NOW()
        WHERE id = $1 AND status IN ('queued', 'needs_approval', 'running')
        RETURNING *
        """,
        job_id,
    )
    return _row(rec) if rec else None


async def set_notify(job_id: uuid.UUID) -> bool:
    """Ask for a Pushover on finish. False if the job already finished (caller reads the result)."""
    rec = await get_pool().fetchval(
        """
        UPDATE jobs SET notify = TRUE
        WHERE id = $1 AND status IN ('queued', 'running')
        RETURNING id
        """,
        job_id,
    )
    return rec is not None


async def approve_job(job_id: uuid.UUID) -> dict[str, Any] | None:
    rec = await get_pool().fetchrow(
        "UPDATE jobs SET status = 'queued' WHERE id = $1 AND status = 'needs_approval' RETURNING *",
        job_id,
    )
    return _row(rec) if rec else None


async def update_outcome(
    job_id: uuid.UUID, result: str | None, meta_patch: dict[str, Any]
) -> dict[str, Any] | None:
    """Replace the result text and merge keys into meta (e.g. the PR a change job opened)."""
    rec = await get_pool().fetchrow(
        "UPDATE jobs SET result = COALESCE($2, result), meta = meta || $3::jsonb "
        "WHERE id = $1 RETURNING *",
        job_id,
        result,
        json.dumps(meta_patch),
    )
    return _row(rec) if rec else None


async def awaiting_ci() -> list[dict[str, Any]]:
    """Recent change jobs with an open PR whose CI result hasn't been handled yet."""
    rows = await get_pool().fetch(
        """
        SELECT * FROM jobs
        WHERE profile = 'change' AND status = 'succeeded'
          AND meta ? 'pr_url' AND meta ? 'head_sha' AND NOT meta ? 'ci_state'
          AND finished_at > NOW() - INTERVAL '6 hours'
        ORDER BY finished_at
        """
    )
    return [_row(r) for r in rows]
