"""CI watcher for bot PRs (plans/07): wait for checks, notify when green, fix when red.

Every POLL_SECONDS it looks at recent change jobs whose PR is waiting on CI. A PR that was closed
or merged meanwhile is left alone for good (ci_state closed or merged): no notice, no fix-up.
Results come from the gateway (`/github/repos/{owner}/{repo}/ci?sha=`, Pryce's token, which can
read Actions):
- success: mark the job and send one "PR ready" Pushover.
- failure: start a fix-up follow-up on the same branch with the failing log tails, up to
  MAX_FIX_ATTEMPTS per PR (fix-ups count against the daily cap); after that, notify and stop.
- no runs after NO_CI_AFTER: the repo has no CI; stop watching.
Each tick also refreshes the snapshot of every PR a board card points at (board.prs).
Runs only where the worker is configured (WORKER_KEY set), so not on GCP.
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from app.board import prs
from app.board import store as board_store
from app.config import settings
from app.http_client import get_client
from app.jobs import service, store
from app.learning import service as learning

logger = logging.getLogger(__name__)

POLL_SECONDS = 90
MAX_FIX_ATTEMPTS = 2
NO_CI_AFTER = timedelta(minutes=15)
PR_SYNC_EVERY = 7  # ticks: about every 10 minutes
# Each PR is fetched at most every prs.SNAPSHOT_AGE, so this caps a burst (first deploy, many
# PRs linked at once), not the steady rate.
SNAPSHOTS_PER_PASS = 10
_task: asyncio.Task | None = None


async def _ci(repo: str, sha: str) -> dict[str, Any] | None:
    owner, name = repo.split("/", 1)
    try:
        resp = await get_client().get(
            f"{settings.gateway_url}/github/repos/{owner}/{name}/ci",
            params={"sha": sha},
            headers={"X-API-Key": settings.gateway_api_key},
            timeout=60.0,
        )
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPError:
        logger.warning("CI status check failed for %s@%s", repo, sha[:7], exc_info=True)
        return None


async def pull_request(repo: str, number: int) -> dict[str, Any] | None:
    owner, name = repo.split("/", 1)
    try:
        resp = await get_client().get(
            f"{settings.gateway_url}/github/repos/{owner}/{name}/pulls/{number}",
            headers={"X-API-Key": settings.gateway_api_key},
            timeout=30.0,
        )
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPError:
        logger.warning("PR state check failed for %s#%s", repo, number, exc_info=True)
        return None


def _fix_instruction(ci: dict[str, Any], attempt: int) -> str:
    # The reply format (the JSON block) comes from the follow-up rule create_follow_up appends;
    # whether tests can run comes from the worker's shell note.
    parts = [
        f"CI failed on the pull request you opened (fix attempt {attempt} of {MAX_FIX_ATTEMPTS}). "
        "Fix the cause in the files, touching only what's needed. Read the errors carefully, and "
        "if a note at the top says you can run tests, run the ones that failed.",
    ]
    for f in ci.get("failures", [])[:3]:
        steps = ", ".join(f.get("failed_steps") or []) or "unknown step"
        parts.append(f"\n## {f.get('workflow')} / {f.get('job')} (failed: {steps})\n"
                     f"```\n{(f.get('log_tail') or '')[-3500:]}\n```")
    return "\n".join(parts)


async def _settle(job: dict[str, Any], patch: dict[str, Any]) -> None:
    await store.update_outcome(job["id"], None, patch)
    if job.get("parent_id"):
        await store.settle_ancestors(job["id"], patch["ci_state"])


async def _record_failure(job_id) -> None:
    if job := await store.get_job(job_id):
        await learning.record(job)


async def _check(job: dict[str, Any]) -> None:
    meta = job.get("meta") or {}
    params = job.get("params") or {}
    if meta.get("pr_number"):
        pr = await pull_request(params["repo"], int(meta["pr_number"]))
        if pr is None:
            return  # try again next tick
        if pr.get("state") != "open":
            # Closed or merged: nothing to report or fix (a fix-up would only reopen the work).
            state = "merged" if pr.get("merged") else "closed"
            await _settle(job, {"ci_state": state})
            return
    ci = await _ci(params["repo"], meta["head_sha"])
    if ci is None:
        return
    state = ci.get("state")
    if state == "pending":
        return
    if state == "none":
        if datetime.now(UTC) - job["finished_at"] > NO_CI_AFTER:
            await _settle(job, {"ci_state": "no_ci"})
        return
    runs = [r.get("url") for r in ci.get("runs", [])]
    if state == "success":
        await _settle(job, {"ci_state": "success", "ci_runs": runs})
        await service.notify_simple(
            f"PR #{meta.get('pr_number')} ready · {params['repo'].split('/')[-1]}",
            f"{job['title']}. CI passed.",
            meta.get("pr_url"),
            f"Review PR #{meta.get('pr_number')} on GitHub",
        )
        return

    attempt = int(params.get("ci_fix_attempt") or 0) + 1
    if attempt > MAX_FIX_ATTEMPTS:
        await _settle(job, {"ci_state": "failure", "ci_runs": runs})
        await _record_failure(job["id"])
        await service.notify_simple(
            f"PR #{meta.get('pr_number')} needs you",
            f"{job['title']}. CI still fails after {MAX_FIX_ATTEMPTS} automatic fixes.",
            meta.get("pr_url"),
            f"Open PR #{meta.get('pr_number')} on GitHub",
        )
        return
    try:
        fix = await service.create_follow_up(
            job["id"],
            _fix_instruction(ci, attempt),
            requested_by=f"ci-fix:{job['id']}",
            extra_params={"ci_fix_attempt": attempt},
        )
    except service.JobError as e:
        logger.warning("CI fix-up for job %s not started: %s", job["id"], e)
        await _settle(job, {"ci_state": "failure", "ci_fix_error": str(e)})
        await _record_failure(job["id"])
        return
    await store.update_outcome(
        job["id"], None, {"ci_state": "fixing", "ci_fix_job": str(fix["id"]), "ci_runs": runs}
    )
    logger.info("Job %s: CI failed, fix-up %s (attempt %d)", job["id"], fix["id"], attempt)


async def _snapshot(ref: dict[str, Any], old: dict[str, Any] | None) -> None:
    """Store one PR's state, checks and review. A failed gateway call leaves the old snapshot,
    fetched time included. Passing checks on an unchanged head aren't asked again: the gateway
    fetches log tails for failing runs, so only running and failing ones cost more than one call."""
    pr = await _pr(ref["repo"], ref["number"])
    if pr is None:
        return
    sha = prs.head_sha(pr)
    old_checks = (old or {}).get("checks") or {}
    if sha and old and old["head_sha"] == sha and old_checks.get("state") == "passing":
        pr_checks = old_checks
    elif sha:
        ci = await _ci(ref["repo"], sha)
        if ci is None:
            return
        pr_checks = prs.checks(ci)
    else:
        pr_checks = prs.checks(None)
    review = prs.review(await board_store.latest_review(ref["repo"], ref["number"]))
    await board_store.save_pr_snapshot(
        prs.key(ref["repo"], ref["number"]), prs.snapshot(pr, pr_checks, review)
    )


async def snapshot_prs() -> None:
    """Refresh the PR snapshots cards read, at most SNAPSHOTS_PER_PASS per tick."""
    from app.board.service import DONE_WINDOW  # lazy: the board service imports the jobs service

    due = prs.due(
        await board_store.items(),
        await board_store.chains(),
        await board_store.active_jobs(),
        await board_store.pr_snapshots(),
        datetime.now(UTC),
        DONE_WINDOW,
    )
    for ref, old in due[:SNAPSHOTS_PER_PASS]:
        try:
            await _snapshot(ref, old)
        except Exception:
            logger.exception("PR snapshot failed for %s#%s", ref["repo"], ref["number"])


async def _loop() -> None:
    from app.backlog import service as backlog  # lazy: backlog imports the jobs service

    tick = 0
    while True:
        try:
            for job in await store.awaiting_ci():
                await _check(job)
        except Exception:
            logger.exception("CI watcher tick failed")
        try:
            await snapshot_prs()
        except Exception:
            logger.exception("PR snapshot pass failed")
        # Jobs paused by Pryce's plan limit resume once it resets (plans/10 P1.2).
        try:
            if resumed := await service.resume_rate_limited():
                logger.info("Resumed %d job(s) after the plan limit reset", resumed)
        except Exception:
            logger.exception("Plan-limit resume tick failed")
        # Merged or closed PRs move their backlog items along; minutes of lag are fine.
        if tick % PR_SYNC_EVERY == 0:
            try:
                await backlog.sync_pr_states()
            except Exception:
                logger.exception("Backlog PR sync failed")
        tick += 1
        await asyncio.sleep(POLL_SECONDS)


async def start() -> None:
    global _task
    if not settings.worker_key or not settings.database_url:
        logger.info("CI watcher disabled (no worker configured)")
        return
    _task = asyncio.create_task(_loop())
    logger.info("CI watcher started (every %ss)", POLL_SECONDS)


async def stop() -> None:
    if _task is not None:
        _task.cancel()

