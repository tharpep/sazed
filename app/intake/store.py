"""Postgres access for the intake (migration 022): trigger log, threads, owning-job lookups."""

import uuid
from typing import Any

from app.db import get_pool
from app.jobs.store import get_job

_CHAIN_TAIL = """
    WITH RECURSIVE chain AS (
        SELECT id, created_at FROM jobs WHERE id = ANY($1::uuid[])
        UNION ALL
        SELECT j.id, j.created_at FROM jobs j JOIN chain c ON j.parent_id = c.id
    )
    SELECT id FROM chain ORDER BY created_at DESC LIMIT 1
"""


async def _tail(root_ids: list[uuid.UUID]) -> dict[str, Any] | None:
    if not root_ids:
        return None
    tail = await get_pool().fetchval(_CHAIN_TAIL, root_ids)
    return await get_job(tail) if tail else None


async def owner_of_pr(repo: str, number: int) -> dict[str, Any] | None:
    """The newest job in the chain that opened this PR, or None when Sazed didn't open it."""
    roots = await get_pool().fetch(
        """
        SELECT id FROM jobs
        WHERE kind = 'code_task' AND profile = 'change'
          AND params->>'repo' = $1 AND meta->>'pr_number' = $2
        """,
        repo,
        str(number),
    )
    return await _tail([r["id"] for r in roots])


async def thread(thread_key: str) -> dict[str, Any] | None:
    rec = await get_pool().fetchrow(
        "SELECT * FROM intake_threads WHERE thread_key = $1", thread_key
    )
    return dict(rec) if rec else None


async def owner_of_thread(thread_key: str) -> dict[str, Any] | None:
    found = await thread(thread_key)
    if not found or not found["job_id"]:
        return None
    return await _tail([found["job_id"]])


async def remember_thread(
    thread_key: str, *, job_id: uuid.UUID | None = None, item_id: uuid.UUID | None = None
) -> None:
    await get_pool().execute(
        """
        INSERT INTO intake_threads (thread_key, job_id, item_id) VALUES ($1, $2, $3)
        ON CONFLICT (thread_key) DO UPDATE
        SET job_id = COALESCE(intake_threads.job_id, EXCLUDED.job_id),
            item_id = COALESCE(intake_threads.item_id, EXCLUDED.item_id)
        """,
        thread_key,
        job_id,
        item_id,
    )


async def record(
    *,
    source: str,
    event: str,
    action: str,
    actor: str,
    thread_key: str,
    decision: str,
    reason: str,
    action_taken: str | None = None,
    job_id: uuid.UUID | None = None,
    item_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    rec = await get_pool().fetchrow(
        """
        INSERT INTO intake_triggers
            (source, event, action, actor, thread_key, decision, reason, action_taken,
             job_id, item_id)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
        RETURNING *
        """,
        source,
        event,
        action,
        actor,
        thread_key,
        decision,
        reason,
        action_taken,
        job_id,
        item_id,
    )
    return dict(rec)


async def recent(limit: int = 50) -> list[dict[str, Any]]:
    rows = await get_pool().fetch(
        "SELECT * FROM intake_triggers ORDER BY received_at DESC LIMIT $1", limit
    )
    return [dict(r) for r in rows]
