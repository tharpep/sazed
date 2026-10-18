"""CI watcher for bot PRs (plans/07): wait for checks, notify when green, fix when red.

Every POLL_SECONDS it looks at recent change jobs whose PR is waiting on CI. Results come from
the gateway (`/github/repos/{owner}/{repo}/ci?sha=`, Pryce's token, which can read Actions):
- success: mark the job and send one "PR ready" Pushover.
- failure: start a fix-up follow-up on the same branch with the failing log tails, up to
  MAX_FIX_ATTEMPTS per PR (fix-ups count against the daily cap); after that, notify and stop.
- no runs after NO_CI_AFTER: the repo has no CI; stop watching.
Runs only where the worker is configured (WORKER_KEY set), so not on GCP.
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from app.config import settings
from app.http_client import get_client
from app.jobs import service, store

logger = logging.getLogger(__name__)

POLL_SECONDS = 90
MAX_FIX_ATTEMPTS = 2
NO_CI_AFTER = timedelta(minutes=15)
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


def _fix_instruction(ci: dict[str, Any], attempt: int) -> str:
    parts = [
        f"CI failed on the pull request you opened (fix attempt {attempt} of {MAX_FIX_ATTEMPTS}). "
        "Fix the cause in the files, touching only what's needed. You still can't run anything, "
        "so read the errors carefully. Then reply with the complete, updated PR description.",
    ]
    for f in ci.get("failures", [])[:3]:
        steps = ", ".join(f.get("failed_steps") or []) or "unknown step"
        parts.append(f"\n## {f.get('workflow')} / {f.get('job')} (failed: {steps})\n"
                     f"```\n{(f.get('log_tail') or '')[-3500:]}\n```")
    return "\n".join(parts)


async def _check(job: dict[str, Any]) -> None:
    meta = job.get("meta") or {}
    params = job.get("params") or {}
    ci = await _ci(params["repo"], meta["head_sha"])
    if ci is None:
        return
    state = ci.get("state")
    if state == "pending":
        return
    if state == "none":
        if datetime.now(UTC) - job["finished_at"] > NO_CI_AFTER:
            await store.update_outcome(job["id"], None, {"ci_state": "no_ci"})
        return
    runs = [r.get("url") for r in ci.get("runs", [])]
    if state == "success":
        await store.update_outcome(job["id"], None, {"ci_state": "success", "ci_runs": runs})
        await service.notify_simple(
            f"PR ready: {job['title']}", f"CI passed. {meta.get('pr_url', '')}"
        )
        return

    attempt = int(params.get("ci_fix_attempt") or 0) + 1
    if attempt > MAX_FIX_ATTEMPTS:
        await store.update_outcome(job["id"], None, {"ci_state": "failure", "ci_runs": runs})
        await service.notify_simple(
            f"PR needs you: {job['title']}",
            f"CI still failing after {MAX_FIX_ATTEMPTS} fix attempts. {meta.get('pr_url', '')}",
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
        await store.update_outcome(job["id"], None, {"ci_state": "failure", "ci_fix_error": str(e)})
        return
    await store.update_outcome(
        job["id"], None, {"ci_state": "fixing", "ci_fix_job": str(fix["id"]), "ci_runs": runs}
    )
    logger.info("Job %s: CI failed, fix-up %s (attempt %d)", job["id"], fix["id"], attempt)


async def _loop() -> None:
    while True:
        try:
            for job in await store.awaiting_ci():
                await _check(job)
        except Exception:
            logger.exception("CI watcher tick failed")
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

