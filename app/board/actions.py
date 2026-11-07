"""Actions on board cards: which ones a card offers, from its job's state, and running one."""

import asyncio
import uuid
from typing import Any

import httpx

from app.config import settings
from app.http_client import get_client
from app.jobs import service as jobs_service
from app.jobs import store as jobs
from app.learning import service as learning

LABELS = {
    "approve": "Approve",
    "decline": "Decline",
    "answer": "Answer",
    "steer": "Steer",
    "stop": "Stop",
    "resume": "Resume",
    "merge": "Merge",
}
TAKES_TEXT = {"answer", "steer"}
SESSION_WAIT_S = 20.0


class ActionError(ValueError):
    """The action isn't one this card offers, or it couldn't be carried out."""


def offered(job: dict[str, Any] | None) -> list[str]:
    if job is None:
        return []
    status = job["status"]
    meta = job.get("meta") or {}
    if status == "needs_approval":
        return ["approve", "decline"]
    if status == "needs_input":
        return ["answer", "stop"]
    if status == "running":
        return ["steer", "stop"]
    if status == "queued":
        return ["stop"]
    if status == "paused" and meta.get("pause_reason") != "rate_limit":
        return ["resume", "stop"]
    ready = meta.get("ci_state") in ("success", "no_ci") and not meta.get("pr_draft")
    if status == "succeeded" and meta.get("pr_number") and ready:
        return ["merge"]
    return []


def describe(job: dict[str, Any] | None) -> list[dict[str, Any]]:
    return [{"id": a, "label": LABELS[a], "takes_text": a in TAKES_TEXT} for a in offered(job)]


async def _stop(job: dict[str, Any]) -> dict[str, Any]:
    stopped = await jobs.cancel_job(job["id"])
    if stopped is None:
        raise ActionError("the job already finished")
    await learning.record(stopped)
    return stopped


async def _session_after_stop(job_id: uuid.UUID) -> dict[str, Any]:
    """The worker reports a stopped job's session a few seconds after the stop lands."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + SESSION_WAIT_S
    while True:
        job = await jobs.get_job(job_id)
        if job and job.get("agent_session_id"):
            return job
        if loop.time() >= deadline:
            raise ActionError("stopped, but the worker reported no session to redirect")
        await asyncio.sleep(1.0)


async def _merge(job: dict[str, Any]) -> dict[str, Any]:
    owner, _, name = (job.get("params") or {})["repo"].partition("/")
    number = (job.get("meta") or {})["pr_number"]
    try:
        resp = await get_client().put(
            f"{settings.gateway_url}/github/repos/{owner}/{name}/pulls/{number}/merge",
            headers={"X-API-Key": settings.gateway_api_key},
            timeout=40.0,
        )
    except httpx.HTTPError as e:
        raise ActionError(f"the gateway is unreachable: {e}") from e
    if resp.status_code >= 400:
        raise ActionError(resp.json().get("detail", resp.text))
    await jobs.update_outcome(job["id"], None, {"ci_state": "merged"})
    return resp.json()


async def run(job_id: uuid.UUID, action: str, text: str = "") -> dict[str, Any]:
    job = await jobs.get_job(job_id)
    if job is None:
        raise ActionError("no job behind this card")
    if action not in offered(job):
        raise ActionError(f"{action} isn't available while the job is {job['status']}")
    if action in TAKES_TEXT and len(text.strip()) < 2:
        raise ActionError(f"{action} needs a message")
    try:
        if action == "approve":
            approved = await jobs.approve_job(job_id)
            if approved is None:
                raise ActionError("the job is no longer waiting for approval")
            return approved
        if action in ("decline", "stop"):
            return await _stop(job)
        if action == "resume":
            return await jobs_service.resume_job(job_id, "console")
        if action == "answer":
            return await jobs_service.create_follow_up(job_id, text, "console", approved=True)
        if action == "steer":
            await _stop(job)
            await _session_after_stop(job_id)
            return await jobs_service.create_follow_up(job_id, text, "console", approved=True)
        return await _merge(job)
    except jobs_service.JobError as e:
        raise ActionError(str(e)) from e
