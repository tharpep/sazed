"""Worker API (plans/07): the sazed-worker container claims jobs, streams events, posts results.

Authenticated with X-Worker-Key (settings.worker_key), not the console API key, so the worker
can reach only these three routes. Disabled (503) when WORKER_KEY is empty.
"""

import hmac
import logging
import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app.config import settings
from app.jobs import service, store

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
    return {
        k: job[k] for k in ("id", "kind", "agent", "model", "profile", "prompt", "params")
    } | {"resume_session_id": await _resume_id(job)}


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


@router.post("/jobs/{job_id}/finish")
async def finish(job_id: uuid.UUID, body: FinishRequest):
    job = await store.finish_job(
        job_id, body.status, body.result, body.error, body.agent_session_id, body.meta
    )
    if job is None:
        # Already cancelled or timed out on this side; the worker's result is dropped.
        return {"recorded": False}
    logger.info("Job %s finished: %s", job_id, body.status)
    await service.on_finished(job)
    return {"recorded": True}
