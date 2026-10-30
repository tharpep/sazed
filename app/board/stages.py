"""Which stage a card is in, and what happens next (plans/13). Pure functions over a backlog item's
status and the last job in its chain, so a card can't drift from the facts.

Stages are fixed and fit any kind of work; code detail stays on the card as fields.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

STAGES = ("inbox", "next", "doing", "needs_me", "done")

# store.awaiting_ci stops looking after this, so a PR still without a result won't get one.
_CI_GIVES_UP = timedelta(hours=6)

Stage = tuple[str, str]


def job_stage(job: dict[str, Any], now: datetime | None = None) -> Stage:
    """Stage of a job and the PR it may have opened."""
    status = job["status"]
    meta = job.get("meta") or {}
    if status == "needs_approval":
        return "needs_me", "Approve the job"
    if status == "needs_input":
        return "needs_me", "Answer its question"
    if status == "queued":
        return "doing", "Queued for the worker"
    if status == "running":
        return "doing", "Running"
    if status == "paused":
        if meta.get("pause_reason") == "rate_limit":
            return "doing", "Paused until the limit resets"
        return "needs_me", "Resume it or cancel it"
    if status in ("failed", "timed_out"):
        return "needs_me", "The job stopped; retry or dismiss it"
    if status == "cancelled":
        return "needs_me", "Cancelled; retry or dismiss it"
    return _finished(job, meta, now or datetime.now(UTC))


def _finished(job: dict[str, Any], meta: dict[str, Any], now: datetime) -> Stage:
    if not meta.get("pr_number"):
        return "needs_me", "Finished with no PR; read the result"
    if meta.get("pr_draft"):
        return "needs_me", "Draft PR: tests were failing"
    ci = meta.get("ci_state")
    if ci == "merged":
        return "done", "Merged"
    if ci == "closed":
        return "needs_me", "PR closed without merging"
    if ci in ("success", "no_ci"):
        return "needs_me", "Ready to merge"
    if ci == "failure":
        return "needs_me", "CI still failing after the fix-ups"
    if ci == "fixing":
        return "doing", "CI fix-up running"
    finished = job.get("finished_at")
    if finished and now - finished > _CI_GIVES_UP:
        return "needs_me", "No CI result; check the PR"
    return "doing", "Waiting for CI"


def item_stage(
    item: dict[str, Any], job: dict[str, Any] | None, now: datetime | None = None
) -> Stage:
    """Stage of a backlog item. `job` is the last job in its chain, the one with the live state."""
    status = item["status"]
    if status == "done":
        return "done", "Done"
    # An open item may still hold a job from an earlier try (a closed PR puts it back in Open).
    if status == "approved" and job is not None:
        return job_stage(job, now)
    if status == "shortlisted" or item.get("rank") is not None:
        return "next", "Approve it to start"
    return "inbox", "Rank it or dismiss it"
