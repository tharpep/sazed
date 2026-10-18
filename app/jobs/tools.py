"""Handlers for Sazed's job tools (INTERNAL tools; definitions live in agent/tools.py)."""

import json
import uuid
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

from app.config import settings
from app.jobs import service, store

JOB_TOOL_NAMES = frozenset(
    {"research", "second_opinion", "delegate_code_task", "continue_job", "job_status"}
)

# "<kind>:<session id>" for the current turn, set by the agent loop: kind is "session" for
# Pryce's chats, otherwise the session type (automation, think, claude). A job that outlives the
# tool call posts its result back into this session (service._post_to_session), and the kind
# decides whether the job counts against the daily cap (service.counts_toward_cap).
current_session: ContextVar[str | None] = ContextVar("current_session", default=None)

_RESULT_LIMIT = 12_000


def _requested_by() -> str:
    return current_session.get() or "sazed"


def _elapsed(job: dict[str, Any]) -> int | None:
    start = job.get("started_at") or job.get("created_at")
    end = job.get("finished_at") or datetime.now(UTC)
    return int((end - start).total_seconds()) if start else None


def _report(job: dict[str, Any]) -> str:
    out: dict[str, Any] = {
        "job_id": str(job["id"]),
        "status": job["status"],
        "model": job["model"],
        "seconds": _elapsed(job),
    }
    if job["status"] == "succeeded":
        out["result"] = (job.get("result") or "")[:_RESULT_LIMIT]
    elif job.get("error"):
        out["error"] = job["error"]
    if (job.get("params") or {}).get("save_to_kb"):
        out["saved_to_kb"] = job["status"] == "succeeded"
    return json.dumps(out)


async def _wait_or_hand_off(job: dict[str, Any]) -> str:
    """Wait a while for the answer; if it isn't ready, hand back the id and notify on finish."""
    done = await service.wait_for(job["id"], settings.research_wait_seconds)
    if done is None and await store.set_notify(job["id"]):
        return json.dumps(
            {
                "job_id": str(job["id"]),
                "status": "running",
                "note": "Still working. When it finishes, Pryce gets a Pushover and the answer is "
                "added to this conversation. job_status fetches it sooner.",
            }
        )
    return _report(done or await store.get_job(job["id"]))


async def handle(name: str, args: dict[str, Any]) -> str:
    try:
        if name == "research":
            job = await service.create_research(
                args["question"],
                args.get("depth") or "quick",
                requested_by=_requested_by(),
                model=args.get("model"),
                context=args.get("context") or "",
                save_to_kb=bool(args.get("save_to_kb")),
            )
            return await _wait_or_hand_off(job)

        if name == "second_opinion":
            job = await service.create_second_opinion(
                args["subject"],
                requested_by=_requested_by(),
                model=args.get("model"),
                context=args.get("context") or "",
            )
            return await _wait_or_hand_off(job)

        if name == "delegate_code_task":
            job = await service.create_code_task(
                args["repo"],
                args["instruction"],
                args.get("mode") or "review",
                requested_by=_requested_by(),
                model=args.get("model"),
                context=args.get("context") or "",
            )
            return await _wait_or_hand_off(job)

        if name == "continue_job":
            job = await service.create_follow_up(
                uuid.UUID(str(args["job_id"])), args["instruction"], requested_by=_requested_by()
            )
            return await _wait_or_hand_off(job)

        if name == "job_status":
            job = await store.get_job(uuid.UUID(str(args["job_id"])))
            return _report(job) if job else "Error: no job with that id."
    except service.JobError as e:
        return f"Error: {e}"
    except (KeyError, ValueError) as e:
        return f"Error: {e}"
    return f"Unknown job tool: {name}"
