"""Reads for the board: items with their job chains, and active jobs that have no item."""

import json
from typing import Any

from app.db import get_pool

# A job chain is the item's job plus every follow-up under it (fix-ups, resumes, continues).
_CHAINS = """
    WITH RECURSIVE chain AS (
        SELECT b.id AS item_id, b.job_id, 0 AS depth
        FROM backlog_items b
        WHERE b.job_id IS NOT NULL AND b.status <> ALL($1::text[])
        UNION ALL
        SELECT c.item_id, j.id, c.depth + 1
        FROM chain c JOIN jobs j ON j.parent_id = c.job_id
    )
    SELECT c.item_id, c.depth, j.id, j.status, j.title, j.kind, j.requested_by, j.params, j.meta,
           j.created_at, j.started_at, j.finished_at
    FROM chain c JOIN jobs j ON j.id = c.job_id
    ORDER BY c.item_id, c.depth, j.created_at
"""

_ACTIVE = ("queued", "running", "needs_approval", "needs_input", "paused")

OFF_BOARD = ("dismissed", "stale")


def _job(rec) -> dict[str, Any]:
    d = dict(rec)
    for field in ("params", "meta"):
        if isinstance(d.get(field), str):
            d[field] = json.loads(d[field])
    return d


async def items() -> list[dict[str, Any]]:
    rows = await get_pool().fetch(
        """
        SELECT id, type, status, title, repos, effort, impact, rank, job_id, created_at, updated_at
        FROM backlog_items WHERE status <> ALL($1::text[])
        ORDER BY rank NULLS LAST, created_at DESC
        """,
        list(OFF_BOARD),
    )
    return [dict(r) for r in rows]


async def chains() -> dict[Any, list[dict[str, Any]]]:
    """Each item's jobs, first to last."""
    result: dict[Any, list[dict[str, Any]]] = {}
    for rec in await get_pool().fetch(_CHAINS, list(OFF_BOARD)):
        job = _job(rec)
        result.setdefault(job.pop("item_id"), []).append(job)
    return result


async def active_jobs() -> list[dict[str, Any]]:
    """Unfinished jobs nothing has continued (a resumed job leaves its paused parent behind)."""
    rows = await get_pool().fetch(
        """
        SELECT j.id, j.status, j.title, j.kind, j.requested_by, j.params, j.meta,
               j.created_at, j.started_at, j.finished_at
        FROM jobs j
        WHERE j.status = ANY($1::text[])
          AND NOT EXISTS (SELECT 1 FROM jobs c WHERE c.parent_id = j.id)
        ORDER BY j.created_at DESC
        """,
        list(_ACTIVE),
    )
    return [_job(r) for r in rows]
