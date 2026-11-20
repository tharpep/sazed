"""Worker API (plans/07): the sazed-worker container claims jobs, streams events, posts results.

Authenticated with X-Worker-Key (settings.worker_key), not the console API key, so the worker
can reach only these three routes. Disabled (503) when WORKER_KEY is empty.
"""

import hmac
import logging
import re
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError

from app import policy
from app.board import dispatch
from app.claude_usage import stats as usage_stats
from app.claude_usage import store as usage_store
from app.config import settings
from app.jobs import change_result, ci_watch, service, store

logger = logging.getLogger(__name__)


def verify_worker_key(request: Request) -> None:
    if not settings.worker_key:
        raise HTTPException(503, "Worker API disabled (WORKER_KEY not set)")
    key = request.headers.get("X-Worker-Key") or ""
    if not hmac.compare_digest(key, settings.worker_key):
        raise HTTPException(401, "Invalid or missing worker key")


router = APIRouter(dependencies=[Depends(verify_worker_key)])


class ClaimRequest(BaseModel):
    agents: list[Literal["agy", "claude"]] = Field(..., min_length=1)
    worker: str = Field("", max_length=100)


class EventsRequest(BaseModel):
    events: list[dict[str, Any]] = Field(default_factory=list, max_length=200)


class FinishRequest(BaseModel):
    status: Literal["succeeded", "failed", "timed_out"]
    result: str | None = Field(None, max_length=200_000)
    error: str | None = Field(None, max_length=10_000)
    agent_session_id: str | None = Field(None, max_length=200)
    meta: dict[str, Any] = Field(default_factory=dict)
    # change jobs: path -> new content (base64) or null for a deletion
    changes: dict[str, str | None] | None = None


async def _after_finish(jobs: list[dict[str, Any]]) -> None:
    for job in jobs:
        await service.on_finished(job)


@router.post("/claim")
async def claim(body: ClaimRequest):
    """The next queued job for one of the worker's agents, or 204 when there's nothing to do."""
    await _after_finish(await store.expire_stale())
    job = await store.claim_next(list(body.agents))
    if job is None:
        return Response(status_code=204)
    logger.info("Job %s (%s/%s) claimed by %s", job["id"], job["kind"], job["agent"], body.worker)
    payload = {
        k: job[k] for k in ("id", "kind", "agent", "model", "profile", "prompt", "params")
    } | {"resume_session_id": await _resume_id(job), "parent_id": job.get("parent_id")}
    params = job.get("params") or {}
    if params.get("private") and params.get("repo"):
        payload["clone_token"] = await service.clone_token(params["repo"])
    return payload


async def _resume_id(job: dict[str, Any]) -> str | None:
    """A follow-up job continues its parent's agent session."""
    if not job.get("parent_id"):
        return None
    parent = await store.get_job(job["parent_id"])
    return parent.get("agent_session_id") if parent else None


@router.post("/jobs/{job_id}/events")
async def events(job_id: uuid.UUID, body: EventsRequest):
    """Store streamed steps and act as the heartbeat. Tells the worker to stop if cancelled."""
    status = await store.add_events(job_id, body.events)
    if status is None:
        raise HTTPException(404, "Job not found")
    return {"cancel": status != "running"}


async def _pr_outcome(
    job: dict[str, Any], result: str, changes: dict[str, str | None], draft: bool = False
) -> tuple[str, dict[str, Any]]:
    """Turn a change job's files into a PR (as the bot), or a draft for a paused job's work so
    far. Returns the result text with a PR line added, and the meta to record. Runs before the
    job is marked finished, so a finished or paused job already has its PR link."""
    if not changes:
        if draft:
            return result + "\n\n_Paused before any files were changed._", {}
        return result + "\n\n_No files were changed, so no PR was opened._", {}
    try:
        pr = await service.open_pull_request({**job, "result": result}, changes, draft=draft)
    except service.JobError as e:
        logger.warning("Job %s: %s", job["id"], e)
        return result + f"\n\n**The change was made but the PR failed:** {e}", {"pr_error": str(e)}
    verb = "updated" if pr.get("updated") else "opened"
    label = "Draft pull request" if pr.get("draft") else "Pull request"
    return result + f"\n\n**{label} ({verb}):** {pr['url']}", {
        "pr_url": pr["url"],
        "pr_number": pr["number"],
        "branch": pr["branch"],
        "head_sha": pr.get("head_sha"),
        "pr_updated": bool(pr.get("updated")),
        "pr_draft": bool(pr.get("draft")),
    }


async def _land_changes(
    job: dict[str, Any], result: str, changes: dict[str, str | None], draft: bool = False
) -> tuple[str, dict[str, Any]]:
    """A result-only job (a bake-off arm) keeps its files for a later promote instead of
    opening a PR; every other change job opens one."""
    if not (job.get("params") or {}).get("result_only"):
        return await _pr_outcome(job, result, changes, draft=draft)
    if changes:
        await store.save_changes(job["id"], changes)
        note = f"_Result only: {len(changes)} changed file(s) kept, no PR opened._"
    else:
        note = "_Result only: no files were changed._"
    return f"{result}\n\n{note}", {"result_only": True}


@router.post("/jobs/{job_id}/finish")
async def finish(job_id: uuid.UUID, body: FinishRequest):
    result, meta, status = body.result, dict(body.meta), body.status
    # A change job's reply ends with a JSON block, so a question written just before the block
    # still counts as the last line.
    question = (
        service.needs_input_question(change_result.without_trailing_block(result or ""))
        if status == "succeeded"
        else None
    )
    if question:
        # The agent stopped to ask Pryce something: pause the job (session kept) rather than
        # finish it, and open no PR for a change it didn't complete.
        status, meta["question"] = "needs_input", question
    elif status == "succeeded":
        current = await store.get_job(job_id)
        if current and current["profile"] == "change" and current["status"] == "running":
            # The PR description is rendered from the reply's fields; a reply that doesn't fit
            # (after the worker's repair round) keeps its prose so the work isn't lost.
            meta["changed_files"] = len(body.changes or {})
            fields = change_result.parse(result or "")
            if fields:
                result, meta["change_result"] = change_result.render(fields), fields.model_dump()
            else:
                meta["result_format"] = "prose"
            # The worker ran the repo's tests (sazed_worker/tests.py): if new failures remain
            # after its fix round, the PR opens as a draft that lists them.
            failing = (meta.get("tests") or {}).get("left") or []
            draft: dict[str, bool] = {}
            if failing:
                result = (result or "") + service.failing_tests_section(failing)
                draft = {"draft": True}
            result, pr_meta = await _land_changes(
                current, result or "", body.changes or {}, **draft
            )
            meta |= pr_meta
        elif current and current["profile"] == "review" and current["status"] == "running":
            params = current.get("params") or {}
            pr_number = params.get("pr_number")
            repo = params.get("repo")
            if pr_number and repo and result:
                try:
                    review = await service.post_pull_review(repo, pr_number, result)
                    meta |= {"pr_review_id": review.get("id"), "pr_review_url": review.get("url")}
                except service.JobError as e:
                    logger.warning("Job %s failed to post PR review: %s", job_id, e)
                    meta["pr_review_error"] = str(e)
    elif meta.get("stop_reason") in service.PAUSING_STOPS and body.agent_session_id:
        # Stopped, not given up (plans/10 P1.7): a change job pauses, its work so far saved as a
        # draft PR, so resume_job can carry on in the same session and working copy.
        current = await store.get_job(job_id)
        if current and current["profile"] == "change" and current["status"] == "running":
            status, meta["pause_reason"] = "paused", meta["stop_reason"]
            if meta["stop_reason"] == "rate_limit":
                # Resumed by the CI watcher's loop once the plan resets (plans/10 P1.2).
                meta["resume_after"] = (meta.get("rate_limited") or {}).get("resets_at")
            result, pr_meta = await _land_changes(
                {**current, "meta": meta}, result or "", body.changes or {}, draft=True
            )
            meta |= pr_meta
    job = await store.finish_job(job_id, status, result, body.error, body.agent_session_id, meta)
    if job is None:
        # Already cancelled or timed out on this side; the worker's result is dropped (a PR
        # opened just above stays on GitHub for Pryce to close). A cancelled job keeps the
        # agent session the worker reports, so it can be redirected with continue_job.
        await store.keep_session(job_id, body.agent_session_id)
        return {"recorded": False}
    logger.info("Job %s finished: %s", job_id, body.status)
    await service.on_finished(job)
    return {"recorded": True}


class ExportIn(BaseModel):
    repo: str
    base: str = Field(..., min_length=1)
    base_sha: str = Field(..., pattern=r"^[0-9a-f]{40}$")
    title: str = Field(..., min_length=1, max_length=256)
    body: str = Field("", max_length=60_000)
    files: dict[str, str | None]
    modes: dict[str, Literal["100644", "100755", "120000"]] = Field(default_factory=dict)


class ExportRefused(Exception):
    def __init__(self, status: int, reason: str, detail: str):
        super().__init__(detail)
        self.status, self.reason, self.detail = status, reason, detail


GATEWAY_MAX_FILES = 200
GATEWAY_MAX_BASE64_CHARS = 5 * 1024 * 1024 * 4 // 3
# pinned by test_plan_export.py::test_a_branch_whose_history_moved_is_refused_not_pr_closed
_GATEWAY_PR_CLOSED = "not reopening it"
# pinned by test_plan_export.py::test_a_plan_id_that_cant_form_a_branch_is_refused
_PLAN_ID_RE = re.compile(r"^(?!.*\.\.)(?!.*\.$)(?!.*\.lock$)[A-Za-z0-9._-]{1,75}$")


async def _refuse_a_ready_pr(repo: str, number: int | None) -> None:
    """Once Pryce marks a plan's PR ready, the worker key no longer rewrites it."""
    if number is None:
        return
    pr = await ci_watch.pull_request(repo, number)
    if pr is None:
        raise service.GatewayUnreachable(f"couldn't read PR #{number} on {repo}")
    if pr.get("state") == "open" and not pr.get("draft"):
        raise ExportRefused(
            409, "refused", f"PR #{number} was marked ready, so export won't rewrite it"
        )


async def _export(plan: str, body: ExportIn) -> dict[str, Any]:
    if not _PLAN_ID_RE.match(plan):
        raise ExportRefused(422, "refused", f"plan id '{plan}' can't form a branch name")
    found = await dispatch.plan_row(plan)
    if found is None:
        raise ExportRefused(404, "refused", f"plan '{plan}' isn't known here")
    try:
        repo = service.full_repo(found["repo"])
        sent = service.full_repo(body.repo)
    except service.JobError as e:
        raise ExportRefused(422, "refused", str(e)) from e
    if repo.lower() != sent.lower() or body.base != found["base"]:
        raise ExportRefused(
            422,
            "refused",
            f"plan '{plan}' is for {repo} at {found['base']}, not {sent} at {body.base}",
        )
    decision = policy.decide(policy.StartJob("claude", f"dispatch:{plan}", repo=repo))
    if decision.verdict != "allow":
        raise ExportRefused(422, "refused", decision.reason)
    if len(body.files) > GATEWAY_MAX_FILES:
        raise ExportRefused(
            413, "export_too_large", f"too many files ({len(body.files)} > {GATEWAY_MAX_FILES})"
        )
    if sum(len(c or "") for c in body.files.values()) > GATEWAY_MAX_BASE64_CHARS:
        raise ExportRefused(413, "export_too_large", "the changes are larger than 5 MiB")
    try:
        await service.repo_info(repo)
        await _refuse_a_ready_pr(repo, found.get("pr_number"))
        pr = await service.open_plan_pull_request(
            plan,
            repo,
            body.base,
            body.base_sha,
            body.title,
            body.body,
            body.files,
            body.modes,
            found.get("pr_number"),
        )
    except httpx.HTTPError as e:
        raise ExportRefused(
            424, "export_unreachable", f"the gateway couldn't be reached ({type(e).__name__})"
        ) from e
    except service.GatewayUnreachable as e:
        raise ExportRefused(424, "export_unreachable", str(e)) from e
    except service.PullRequestError as e:
        if e.status == 409 and _GATEWAY_PR_CLOSED in e.detail:
            raise ExportRefused(409, "pr_closed", e.detail) from e
        if e.status == 409:
            raise ExportRefused(409, "refused", e.detail) from e
        if e.status >= 500:
            raise ExportRefused(424, "export_unreachable", e.detail) from e
        raise ExportRefused(422, "refused", e.detail) from e
    except service.JobError as e:
        raise ExportRefused(422, "refused", str(e)) from e
    await dispatch.record_export(plan, pr["url"], pr["number"], pr.get("head_sha"))
    logger.info("Plan %s exported to %s", plan, pr["url"])
    return {
        "url": pr["url"],
        "number": pr["number"],
        "branch": pr["branch"],
        "head_sha": pr.get("head_sha"),
        "updated": bool(pr.get("updated")),
        "base_moved": bool(pr.get("base_moved")),
    }


@router.post("/dispatch/plans/{plan}/export")
async def export_plan(plan: str, request: Request):
    """Open or update a dispatch plan's draft PR from integ/<plan>'s changed files (seam 6)."""
    try:
        body = ExportIn.model_validate_json(await request.body())
    except ValidationError as e:
        first = e.errors()[0]
        where = ".".join(str(p) for p in first["loc"]) or "body"
        return JSONResponse(
            {"detail": f"{where}: {first['msg']}", "reason": "refused"}, status_code=422
        )
    try:
        return await _export(plan, body)
    except ExportRefused as e:
        logger.warning("Plan %s export refused (%s): %s", plan, e.reason, e.detail)
        return JSONResponse({"detail": e.detail, "reason": e.reason}, status_code=e.status)


@router.post("/limits/due")
async def limits_due():
    return usage_stats.probe_due(await usage_store.probe_state(), datetime.now(UTC))


class LimitsReading(BaseModel):
    rate_limit_info: dict[str, Any]


@router.post("/limits/reading")
async def limits_reading(body: LimitsReading):
    now = datetime.now(UTC)
    return {"added": await usage_store.add_reading(body.rate_limit_info, now, "probe", "homelab")}
