"""Dispatch jobs (D7): a dispatch plan submitted as one worker job that waits for approval."""

import hashlib
import json
import math
import re
from decimal import Decimal
from typing import Any

import asyncpg
from pydantic import BaseModel, Field, ValidationError

from app import policy
from app.board import dispatch
from app.config import settings
from app.db import get_pool
from app.jobs import ci_watch, service, store

KIND = "dispatch"
OPEN_STATUSES = ("needs_approval", "queued", "running")
MAX_BODY = 1024 * 1024
MAX_DEPTH = 64

# pinned by test_plan_export.py::test_a_plan_id_that_cant_form_a_branch_is_refused
PLAN_ID_RE = re.compile(r"^(?!.*\.\.)(?!.*\.$)(?!.*\.lock$)[A-Za-z0-9._-]{1,75}$")


class PlanConflict(service.JobError):
    """The plan has an open dispatch job, or a PR its next export couldn't update."""


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


def _refuse_constant(name: str) -> None:
    raise ValueError(f"{name} isn't JSON")


def _check_depth(data: Any) -> None:
    level, depth = [data], 0
    while level:
        depth += 1
        if depth > MAX_DEPTH:
            raise service.JobError(f"the plan nests deeper than {MAX_DEPTH} levels")
        level = [
            child
            for node in level
            if isinstance(node, dict | list)
            for child in (node.values() if isinstance(node, dict) else node)
        ]


def parse(raw: bytes) -> Submission:
    """json.loads, like the worker, so both sides hash the same objects."""
    try:
        data = json.loads(raw, parse_constant=_refuse_constant)
        _check_depth(data)
        return Submission.model_validate(data)
    except ValidationError as e:
        first = e.errors()[0]
        where = ".".join(str(p) for p in first["loc"]) or "body"
        raise service.JobError(f"{where}: {first['msg']}") from e
    except RecursionError as e:
        raise service.JobError(f"the plan nests deeper than {MAX_DEPTH} levels") from e
    except service.JobError:
        raise
    except ValueError as e:
        raise service.JobError(f"the body isn't JSON: {e}") from e


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
    outputs = body.plan.get("regen_outputs", [])
    if not isinstance(outputs, list) or not all(isinstance(o, str) for o in outputs):
        raise service.JobError("regen_outputs must be a list of paths")
    files = body.plan.get("interface_files", {})
    if not isinstance(files, dict) or not all(isinstance(c, str) for c in files.values()):
        raise service.JobError("interface_files must map paths to text")
    return ids


def _check_caps(ids: list[str], body: Submission) -> None:
    if set(body.task_caps) != set(ids):
        raise service.JobError("task_caps must name exactly the plan's tasks")
    if not all(math.isfinite(cap) and cap > 0 for cap in body.task_caps.values()):
        raise service.JobError("every task cap must be over 0")
    # pinned by test_caps_that_meet_the_budget_are_accepted
    total = sum(Decimal(str(cap)) for cap in body.task_caps.values())
    if total > Decimal(str(body.budget_usd)):
        raise service.JobError(
            f"the task caps add up to ${total:.2f}, over the ${body.budget_usd:.2f} budget"
        )


def glob_match(path: str, pattern: str) -> bool:
    """sazed-worker's rules.glob_match; both suites run tests/data/owns_glob_vectors.json."""
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.fullmatch("".join(out), path) is not None


def outside_plan(plan: dict[str, Any], paths: list[str]) -> list[str]:
    """The paths the approved plan doesn't let an export change. interface_files are literal."""
    globs = [g for t in plan["tasks"] for g in t.get("owns") or []]
    globs += plan.get("regen_outputs") or []
    literal = set(plan.get("interface_files") or {})

    def allowed(path: str) -> bool:
        if any(part in ("", ".", "..") for part in path.split("/")):
            return False
        return path in literal or any(glob_match(path, g) for g in globs)

    return [p for p in paths if not allowed(p)]


_PR_URL_RE = re.compile(r"^https://github\.com/([^/]+/[^/]+)/pull/\d+$")


async def _refuse_a_stored_pr(plan: str, repo: str, base: str) -> None:
    """The next export updates the plan's stored PR, so it must still be this draft."""
    row = await dispatch.plan_row(plan)
    if row is None or row.get("pr_number") is None:
        return
    number = row["pr_number"]
    # The row's repo and base follow the mirror; the PR's own url and base don't.
    found = _PR_URL_RE.match(row.get("pr_url") or "")
    if not found or found.group(1).lower() != repo.lower():
        raise PlanConflict(f"plan '{plan}' has a PR on another repo ({row.get('pr_url')})")
    pr = await ci_watch.pull_request(found.group(1), number)
    if pr is None:
        raise service.GatewayUnreachable(f"couldn't read PR #{number} on {repo}")
    if pr.get("state") != "open":
        state = "merged" if pr.get("merged") else "closed"
        raise PlanConflict(f"plan '{plan}' has a {state} PR (#{number}); use a new plan id")
    if not pr.get("draft"):
        raise PlanConflict(f"plan '{plan}' has a PR marked ready (#{number}); use a new plan id")
    if pr.get("base") != base:
        raise PlanConflict(f"plan '{plan}' has a PR into {pr.get('base')} (#{number})")


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
    title = re.sub(r"\s+", " ", body.title).strip()
    if not title:
        raise service.JobError("the title is blank")
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
    await _refuse_a_stored_pr(plan, repo, body.base)
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
        "title": title,
        "body": body.body,
        "board_card": body.board_card,
    }
    try:
        async with get_pool().acquire() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", source)
            if await _has_open_job(conn, plan):
                raise PlanConflict(f"plan '{plan}' already has a dispatch job waiting or running")
            job = await store.create_job(
                kind=KIND,
                agent="claude",
                model="",
                profile=KIND,
                title=title,
                prompt=title,
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
