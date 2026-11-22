"""Dispatch jobs (D7): a dispatch plan submitted as one worker job that waits for approval."""

import hashlib
import json
import math
import re
from typing import Any

import asyncpg
from pydantic import BaseModel, Field

from app import policy
from app.board import dispatch
from app.config import settings
from app.db import get_pool
from app.jobs import service, store

KIND = "dispatch"
OPEN_STATUSES = ("needs_approval", "queued", "running")

# pinned by test_plan_export.py::test_a_plan_id_that_cant_form_a_branch_is_refused
PLAN_ID_RE = re.compile(r"^(?!.*\.\.)(?!.*\.$)(?!.*\.lock$)[A-Za-z0-9._-]{1,75}$")


class PlanBusy(service.JobError):
    """The plan already has a dispatch job waiting, queued or running."""


class Submission(BaseModel):
    plan: dict[str, Any]
    config: dict[str, Any] = Field(default_factory=dict)
    repo: str = Field(..., min_length=1, max_length=200)
    base: str = Field(..., min_length=1, max_length=200)
    budget_usd: float = Field(..., gt=0, allow_inf_nan=False)
    task_caps: dict[str, float]
    timeout_s: int = Field(..., ge=600, le=14_400)
    parallel: int = Field(..., ge=1, le=4)
    title: str = Field(..., min_length=1, max_length=256)
    body: str | None = Field(None, max_length=60_000)
    board_card: str | None = Field(None, max_length=100)


def plan_hash(plan: dict[str, Any], config: dict[str, Any]) -> str:
    """sazed-worker computes this with the same expression; both suites pin one literal."""
    text = json.dumps(
        {"plan": plan, "config": config},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _task_ids(plan: str, body: Submission) -> list[str]:
    if body.plan.get("id") != plan or not PLAN_ID_RE.fullmatch(plan):
        raise service.JobError(
            f"plan id '{body.plan.get('id')}' must match the path and be able to form a branch name"
        )
    tasks = body.plan.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise service.JobError("the plan has no tasks")
    ids = [t.get("id") if isinstance(t, dict) else None for t in tasks]
    if not all(isinstance(i, str) and i for i in ids) or len(set(ids)) != len(ids):
        raise service.JobError("every task needs its own id")
    return ids


def _check_caps(ids: list[str], body: Submission) -> None:
    if set(body.task_caps) != set(ids):
        raise service.JobError("task_caps must name exactly the plan's tasks")
    if not all(math.isfinite(cap) and cap > 0 for cap in body.task_caps.values()):
        raise service.JobError("every task cap must be over 0")
    total = sum(body.task_caps.values())
    # pinned by test_caps_that_meet_the_budget_are_accepted
    if round(total, 6) > body.budget_usd:
        raise service.JobError(
            f"the task caps add up to ${total:.2f}, over the ${body.budget_usd:.2f} budget"
        )


async def _has_open_job(conn: Any, plan: str) -> bool:
    found = await conn.fetchval(
        """
        SELECT 1 FROM jobs
        WHERE kind = 'dispatch' AND params->>'plan_id' = $1 AND status = ANY($2::text[])
        LIMIT 1
        """,
        plan,
        list(OPEN_STATUSES),
    )
    return found is not None


async def submit(
    plan: str,
    body: Submission,
    tasks: list[dict[str, Any]],
    project: str | None,
    board_plan: str | None,
) -> dict[str, Any]:
    """Create the plan's dispatch job, always waiting for approval, and link the plan to it."""
    if not settings.worker_key:
        raise service.JobError("the worker isn't configured here (WORKER_KEY is empty)")
    _check_caps(_task_ids(plan, body), body)
    repo = service.full_repo(body.repo)
    source = f"dispatch:{plan}"
    decision = policy.decide(policy.StartJob("claude", source, repo=repo))
    if decision.verdict != "allow":
        raise service.JobError(decision.reason)
    try:
        digest = plan_hash(body.plan, body.config)
    except UnicodeEncodeError as e:
        raise service.JobError("the plan holds text that isn't valid UTF-8") from e
    info = await service.repo_info(repo)
    params = {
        "plan_id": plan,
        "plan": body.plan,
        "config": body.config,
        "plan_hash": digest,
        "repo": repo,
        "base": body.base,
        "private": bool(info.get("private")),
        "budget_usd": body.budget_usd,
        "task_caps": body.task_caps,
        "timeout_s": body.timeout_s,
        "parallel": body.parallel,
        "title": body.title,
        "body": body.body,
        "board_card": body.board_card,
    }
    one_line = re.sub(r"\s+", " ", body.title).strip()
    try:
        async with get_pool().acquire() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", source)
            if await _has_open_job(conn, plan):
                raise PlanBusy(f"plan '{plan}' already has a dispatch job waiting or running")
            job = await store.create_job(
                kind=KIND,
                agent="claude",
                model="",
                profile=KIND,
                title=one_line,
                prompt=one_line,
                params=params,
                requested_by=source,
                status="needs_approval",
                conn=conn,
            )
            stored = job["params"]
            # pinned by test_a_number_jsonb_rewrites_is_refused
            if plan_hash(stored["plan"], stored["config"]) != digest:
                raise service.JobError("the plan holds a number that can't be stored exactly")
            await dispatch.link_job(
                conn, plan, repo, body.base, tasks, project, board_plan, job["id"]
            )
    except asyncpg.DataError as e:
        raise service.JobError(f"the plan can't be stored: {e}") from e
    return job
