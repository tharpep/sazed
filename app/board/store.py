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

OFF_BOARD = ("dismissed", "stale", "parked")


def _job(rec) -> dict[str, Any]:
    d = dict(rec)
    for field in ("params", "meta"):
        if isinstance(d.get(field), str):
            d[field] = json.loads(d[field])
    return d


async def items() -> list[dict[str, Any]]:
    rows = await get_pool().fetch(
        """
        SELECT id, type, status, title, repos, effort, impact, rank, job_id, workstream_id,
               plan_id, plan_section, plan_version,
               session_label, session_branch, pr_repo, pr_number,
               created_at, updated_at, decided_at
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


async def working_jobs() -> list[dict[str, Any]]:
    rows = await get_pool().fetch(
        """
        SELECT id, status, title, agent, model, profile, requested_by, params, meta,
               created_at, started_at
        FROM jobs
        WHERE status IN ('running', 'queued')
        ORDER BY COALESCE(started_at, created_at)
        """
    )
    return [_job(r) for r in rows]


async def questions(job_ids: list[Any]) -> dict[str, str]:
    rows = await get_pool().fetch(
        "SELECT id, meta->>'question' AS question FROM jobs WHERE id = ANY($1::uuid[])",
        job_ids,
    )
    return {str(r["id"]): r["question"] for r in rows if r["question"]}


async def finished_since(hours: int) -> list[dict[str, Any]]:
    rows = await get_pool().fetch(
        """
        SELECT id, status, title, params->>'repo' AS repo, finished_at
        FROM jobs
        WHERE finished_at > NOW() - make_interval(hours => $1)
          AND status IN ('succeeded', 'failed', 'timed_out')
        ORDER BY finished_at DESC
        """,
        hours,
    )
    return [dict(r) for r in rows]


async def pr_snapshots() -> dict[tuple[str, int], dict[str, Any]]:
    rows = await get_pool().fetch(
        """
        SELECT repo, number, state, head_sha, merged_at, additions, deletions, checks,
               review_verdict, review_source, review_url, fetched_at, attempted_at, failures
        FROM pr_snapshots
        """
    )
    return {(r["repo"], r["number"]): dict(r) | {"checks": _checks(r["checks"])} for r in rows}


def _checks(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


async def save_pr_snapshot(key: tuple[str, int], snap: dict[str, Any]) -> None:
    await get_pool().execute(
        """
        INSERT INTO pr_snapshots (repo, number, state, head_sha, merged_at, additions, deletions,
                                  checks, review_verdict, review_source, review_url,
                                  fetched_at, attempted_at, failures)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9, $10, $11, NOW(), NOW(), 0)
        ON CONFLICT (repo, number) DO UPDATE SET
            state = EXCLUDED.state, head_sha = EXCLUDED.head_sha,
            merged_at = EXCLUDED.merged_at, additions = EXCLUDED.additions,
            deletions = EXCLUDED.deletions, checks = EXCLUDED.checks,
            review_verdict = EXCLUDED.review_verdict, review_source = EXCLUDED.review_source,
            review_url = EXCLUDED.review_url, fetched_at = NOW(), attempted_at = NOW(),
            failures = 0
        """,
        key[0],
        key[1],
        snap["state"],
        snap["head_sha"],
        snap["merged_at"],
        snap["additions"],
        snap["deletions"],
        None if snap["checks"] is None else json.dumps(snap["checks"]),
        snap["review_verdict"],
        snap["review_source"],
        snap["review_url"],
    )


async def record_pr_failure(key: tuple[str, int]) -> None:
    """A failed fetch: the last good read, if any, stays as it was."""
    await get_pool().execute(
        """
        INSERT INTO pr_snapshots (repo, number, attempted_at, failures) VALUES ($1, $2, NOW(), 1)
        ON CONFLICT (repo, number) DO UPDATE SET
            attempted_at = NOW(), failures = pr_snapshots.failures + 1
        """,
        key[0],
        key[1],
    )


async def latest_review(repo: str, number: int) -> dict[str, Any] | None:
    """The newest finished review job on a PR; repo is matched lower-case."""
    rec = await get_pool().fetchrow(
        """
        SELECT agent, result, meta FROM jobs
        WHERE profile = 'review' AND status = 'succeeded' AND result IS NOT NULL
          AND lower(params->>'repo') = $1 AND params->>'pr_number' = $2
        ORDER BY finished_at DESC NULLS LAST
        LIMIT 1
        """,
        repo.lower(),
        str(number),
    )
    return _job(rec) if rec else None
