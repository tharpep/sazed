"""Events from outside (ai-context research/agent-stack.md, D1): the gateway verifies and forwards
them, and Sazed's code decides what each one starts. Today that's GitHub pull requests."""

from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.jobs import service, store

router = APIRouter()

_REVIEWED_ACTIONS = ("opened", "synchronize", "ready_for_review")


class PullRequestEvent(BaseModel):
    repo: str = Field(..., min_length=3, max_length=200)
    pr_number: int = Field(..., ge=1)
    action: str = Field(..., max_length=50)
    draft: bool = False
    sender: str = Field("", max_length=100)
    title: str = Field("", max_length=500)
    body: str = Field("", max_length=8000)
    branch: str | None = Field(None, max_length=255)


class EventOutcome(BaseModel):
    status: Literal["queued", "ignored"]
    reason: str | None = None
    job_id: str | None = None


@router.post("/github/pull-request", response_model=EventOutcome)
async def github_pull_request(event: PullRequestEvent):
    if event.action not in _REVIEWED_ACTIONS:
        return EventOutcome(status="ignored", reason=f"action {event.action}")
    if event.draft:
        return EventOutcome(status="ignored", reason="draft")
    if event.sender.endswith("[bot]"):
        return EventOutcome(status="ignored", reason="opened by a bot")
    if await store.pr_review_open(event.repo, event.pr_number):
        return EventOutcome(status="ignored", reason="a review of this PR is already queued")
    instruction = f"Review PR #{event.pr_number}: {event.title}\n\n{event.body}".strip()
    try:
        job = await service.create_code_task(
            event.repo,
            instruction,
            "review",
            "webhook",
            pr_number=event.pr_number,
            branch=event.branch,
        )
    except service.JobError as e:
        raise HTTPException(422, str(e)) from e
    return EventOutcome(status="queued", job_id=str(job["id"]))
