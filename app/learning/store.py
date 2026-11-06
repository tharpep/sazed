"""Postgres access for jobs.failure_class (migration 017) and the guard items it feeds."""

import json
from datetime import datetime
from typing import Any

from app.backlog import store as backlog_store
from app.db import get_pool
from app.jobs import store as jobs_store

GUARD_OPEN = ("new", "shortlisted", "approved")


def _jobs(rows) -> list[dict[str, Any]]:
    return [jobs_store._row(r) for r in rows]


async def set_class(job_id, cls: str) -> bool:
    """False when the job already had a class."""
    result = await get_pool().execute(
        "UPDATE jobs SET failure_class = $2 WHERE id = $1 AND failure_class IS NULL", job_id, cls
    )
    return result.endswith(" 1")


async def hits_since(cls: str, since: datetime | None, limit: int = 50) -> list[dict[str, Any]]:
    rows = await get_pool().fetch(
        """
        SELECT * FROM jobs
        WHERE failure_class = $1 AND ($2::timestamptz IS NULL OR finished_at > $2)
        ORDER BY finished_at DESC LIMIT $3
        """,
        cls,
        since,
        limit,
    )
    return _jobs(rows)


async def guard_items(cls: str) -> list[dict[str, Any]]:
    """Every guard item for the class, newest first."""
    rows = await get_pool().fetch(
        """
        SELECT id, title, status, created_at FROM backlog_items
        WHERE type = 'guard' AND sources @> $1::jsonb ORDER BY created_at DESC
        """,
        json.dumps([f"failure_class:{cls}"]),
    )
    return [dict(r) for r in rows]


async def create_guard(**fields: Any) -> dict[str, Any]:
    return await backlog_store.create_item(type_="guard", source="learning", **fields)


async def open_guards() -> list[dict[str, Any]]:
    rows = await get_pool().fetch(
        """
        SELECT id, title, status, created_at FROM backlog_items
        WHERE type = 'guard' AND status = ANY($1::text[]) ORDER BY created_at DESC
        """,
        list(GUARD_OPEN),
    )
    return [dict(r) for r in rows]


async def class_counts() -> list[dict[str, Any]]:
    rows = await get_pool().fetch(
        """
        SELECT failure_class AS cls, count(*) AS total,
               count(*) FILTER (WHERE finished_at > NOW() - INTERVAL '30 days') AS last_30d
        FROM jobs WHERE failure_class IS NOT NULL GROUP BY failure_class
        """
    )
    return [dict(r) for r in rows]


async def unclassified(limit: int = 50) -> list[dict[str, Any]]:
    rows = await get_pool().fetch(
        "SELECT id, title FROM jobs WHERE failure_class = 'unclassified' "
        "ORDER BY finished_at DESC LIMIT $1",
        limit,
    )
    return [dict(r) for r in rows]


async def unset_settled() -> list[dict[str, Any]]:
    rows = await get_pool().fetch(
        "SELECT * FROM jobs WHERE failure_class IS NULL AND status = ANY($1::text[])",
        list(jobs_store.SETTLED_STATUSES),
    )
    return _jobs(rows)
