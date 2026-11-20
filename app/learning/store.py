"""Postgres access for jobs.failure_class (migration 017), dispatch rows' classes (migration 038)
and the guard items they feed."""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

import asyncpg

from app.backlog import store as backlog_store
from app.db import get_pool
from app.jobs import store as jobs_store

GUARD_OPEN = ("new", "shortlisted", "approved")

_locked: ContextVar[asyncpg.Connection | None] = ContextVar("learning_locked", default=None)


def _db() -> asyncpg.Connection | asyncpg.Pool:
    return _locked.get() or get_pool()


@asynccontextmanager
async def class_lock(cls: str) -> AsyncIterator[None]:
    """One transaction holding the class's advisory lock; guard reads and writes inside use it."""
    async with get_pool().acquire() as conn, conn.transaction():
        await conn.execute(
            "SELECT pg_advisory_xact_lock(hashtext('learning_guard'), hashtext($1))", cls
        )
        token = _locked.set(conn)
        try:
            yield
        finally:
            _locked.reset(token)


def _jobs(rows) -> list[dict[str, Any]]:
    return [jobs_store._row(r) for r in rows]


async def set_class(job_id, cls: str) -> bool:
    """False when the job already had a class."""
    result = await get_pool().execute(
        "UPDATE jobs SET failure_class = $2 WHERE id = $1 AND failure_class IS NULL", job_id, cls
    )
    return result.endswith(" 1")


_LATEST_RED_TASKS = """
    SELECT l.plan, l.task, l.line, l.meta->>'reason' AS reason, to_timestamp(l.ts) AS finished_at,
           dispatch_failure_class(l.state, l.meta) AS cls
    FROM (
        SELECT DISTINCT ON (plan, task) plan, task, state, line, meta, ts
        FROM dispatch_rows WHERE state <> 'cleaned'
        ORDER BY plan, task, ts DESC, id DESC
    ) l
    WHERE dispatch_failure_class(l.state, l.meta) IS NOT NULL
"""


async def dispatch_class(state: str, meta: dict) -> str | None:
    return await get_pool().fetchval(
        "SELECT dispatch_failure_class($1, $2::jsonb)", state, json.dumps(meta)
    )


async def hits_since(cls: str, since: datetime | None, limit: int = 50) -> list[dict[str, Any]]:
    """Jobs of the class and dispatch hits (a task's latest red row, one per plan), newest first."""
    db = _db()
    rows = await db.fetch(
        """
        SELECT * FROM jobs
        WHERE failure_class = $1 AND ($2::timestamptz IS NULL OR finished_at > $2)
        ORDER BY finished_at DESC LIMIT $3
        """,
        cls,
        since,
        limit,
    )
    tasks = await db.fetch(
        f"""
        SELECT DISTINCT ON (h.plan) h.plan, h.task, h.line, h.reason, h.finished_at, p.repo
        FROM ({_LATEST_RED_TASKS}) h
        LEFT JOIN dispatch_plans p ON p.plan = h.plan
        WHERE h.cls = $1 AND ($2::timestamptz IS NULL OR h.finished_at > $2)
        ORDER BY h.plan, h.finished_at DESC
        """,
        cls,
        since,
    )
    hits = _jobs(rows) + [dict(t) for t in tasks]
    hits.sort(key=lambda h: h["finished_at"] or datetime.max.replace(tzinfo=UTC), reverse=True)
    return hits[:limit]


async def guard_items(cls: str) -> list[dict[str, Any]]:
    """Every guard item for the class, newest first."""
    rows = await _db().fetch(
        """
        SELECT id, title, status, created_at FROM backlog_items
        WHERE type = 'guard' AND sources @> $1::jsonb ORDER BY created_at DESC
        """,
        json.dumps([f"failure_class:{cls}"]),
    )
    return [dict(r) for r in rows]


async def create_guard(**fields: Any) -> dict[str, Any]:
    return await backlog_store.create_item(type_="guard", source="learning", db=_db(), **fields)


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
    """Per class, failed jobs plus failed dispatch tasks (each task's latest red row)."""
    rows = await get_pool().fetch(
        f"""
        SELECT cls, count(*) AS total,
               count(*) FILTER (WHERE finished_at > NOW() - INTERVAL '30 days') AS last_30d
        FROM (
            SELECT failure_class AS cls, finished_at FROM jobs WHERE failure_class IS NOT NULL
            UNION ALL
            SELECT cls, finished_at FROM ({_LATEST_RED_TASKS}) t
        ) hits
        GROUP BY cls
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
