"""Handlers for Sazed's job tools (INTERNAL tools; definitions live in agent/tools.py)."""

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from app.config import settings
from app.jobs import service, store

JOB_TOOL_NAMES = frozenset({"research", "job_status"})

_RESULT_LIMIT = 12_000


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
    return json.dumps(out)


async def handle(name: str, args: dict[str, Any]) -> str:
    try:
        if name == "research":
            job = await service.create_research(
                args["question"],
                args.get("depth") or "quick",
                requested_by="sazed",
                model=args.get("model"),
                context=args.get("context") or "",
            )
            done = await service.wait_for(job["id"], settings.research_wait_seconds)
            if done is None and await store.set_notify(job["id"]):
                return json.dumps(
                    {
                        "job_id": str(job["id"]),
                        "status": "running",
                        "note": "Still researching. Pryce gets a Pushover when it's done; "
                        "call job_status with this id to fetch the result.",
                    }
                )
            return _report(done or await store.get_job(job["id"]))

        if name == "job_status":
            job = await store.get_job(uuid.UUID(str(args["job_id"])))
            return _report(job) if job else "Error: no job with that id."
    except service.JobError as e:
        return f"Error: {e}"
    except (KeyError, ValueError) as e:
        return f"Error: {e}"
    return f"Unknown job tool: {name}"
