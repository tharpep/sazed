"""Jobs API for the console and MCP (plans/07): list, inspect, start jobs, cancel."""

import uuid
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.jobs import service, store

router = APIRouter()


class ResearchRequest(BaseModel):
    question: str = Field(..., min_length=3, max_length=4000)
    depth: Literal["quick", "deep"] = "quick"
    model: str | None = None
    save_to_kb: bool = False
    requested_by: Literal["console", "mcp"] = "console"


class SecondOpinionRequest(BaseModel):
    subject: str = Field(..., min_length=3, max_length=20000)
    context: str = Field("", max_length=4000)
    model: str | None = None
    requested_by: Literal["console", "mcp"] = "console"


@router.get("")
async def list_jobs(status: str | None = Query(None), limit: int = Query(50, ge=1, le=200)):
    return {"jobs": await store.list_jobs(status, limit)}


@router.get("/{job_id}")
async def get_job(job_id: uuid.UUID, events: bool = Query(True)):
    job = await store.get_job(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    if events:
        job["events"] = await store.get_events(job_id)
    # For the console and MCP clients: what a long job is doing, and what it asked.
    job["recent_steps"] = service.recent_steps(await store.get_recent_events(job_id))
    asking = job["status"] == "needs_input"
    job["question"] = (job.get("meta") or {}).get("question") if asking else None
    # A queued Claude job behind Pryce's plan limit says so, instead of looking stuck.
    held = job["status"] == "queued" and job["agent"] == "claude"
    job["held_until"] = await store.plan_limit_until() if held else None
    return job


@router.post("/research", status_code=201)
async def start_research(body: ResearchRequest):
    """Queue a research job. It runs in the background; poll GET /jobs/{id} for the result."""
    try:
        job = await service.create_research(
            body.question,
            body.depth,
            body.requested_by,
            model=body.model,
            save_to_kb=body.save_to_kb,
        )
    except service.JobError as e:
        raise HTTPException(422, str(e)) from e
    return job


@router.post("/second-opinion", status_code=201)
async def start_second_opinion(body: SecondOpinionRequest):
    try:
        job = await service.create_second_opinion(
            body.subject, body.requested_by, model=body.model, context=body.context
        )
    except service.JobError as e:
        raise HTTPException(422, str(e)) from e
    return job


class CodeTaskRequest(BaseModel):
    repo: str = Field(..., min_length=1, max_length=200)
    instruction: str = Field(..., min_length=3, max_length=8000)
    mode: Literal["review", "change"] = "review"
    model: str | None = None
    requested_by: Literal["console", "mcp"] = "console"


class FollowUpRequest(BaseModel):
    instruction: str = Field(..., min_length=2, max_length=8000)
    requested_by: Literal["console", "mcp"] = "console"


@router.post("/code-task", status_code=201)
async def start_code_task(body: CodeTaskRequest):
    try:
        job = await service.create_code_task(
            body.repo, body.instruction, body.mode, body.requested_by, model=body.model
        )
    except service.JobError as e:
        raise HTTPException(422, str(e)) from e
    return job


@router.post("/{job_id}/continue", status_code=201)
async def continue_job(job_id: uuid.UUID, body: FollowUpRequest):
    try:
        job = await service.create_follow_up(job_id, body.instruction, body.requested_by)
    except service.JobError as e:
        raise HTTPException(422, str(e)) from e
    return job


class ResumeRequest(BaseModel):
    requested_by: Literal["console", "mcp"] = "console"


@router.post("/{job_id}/resume", status_code=201)
async def resume_job(job_id: uuid.UUID, body: ResumeRequest | None = None):
    """Carry on with a paused change job (plans/10 P1.7); it queues without re-approval."""
    try:
        return await service.resume_job(job_id, (body or ResumeRequest()).requested_by)
    except service.JobError as e:
        raise HTTPException(422, str(e)) from e


@router.post("/{job_id}/approve")
async def approve(job_id: uuid.UUID):
    job = await store.approve_job(job_id)
    if job is None:
        raise HTTPException(409, "Job not found or not waiting for approval")
    return job


@router.post("/{job_id}/cancel")
async def cancel(job_id: uuid.UUID):
    """Stop a job. Its agent session is kept when the worker reports it, so POST
    /jobs/{id}/continue can redirect it with new directions."""
    job = await store.cancel_job(job_id)
    if job is None:
        raise HTTPException(409, "Job not found or already finished")
    return job
