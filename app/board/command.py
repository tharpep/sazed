"""The Home page's summary: what needs Pryce, which agents are working, and what finished."""

import uuid
from datetime import UTC, datetime
from typing import Any

from app.board import actions, dispatch, service, store
from app.jobs import service as jobs_service
from app.jobs import store as jobs

DONE_WINDOW_HOURS = 24
DONE_SHOWN = 5


def working_view(job: dict[str, Any], steps: list[str], now: datetime) -> dict[str, Any]:
    started = job.get("started_at")
    params = job.get("params") or {}
    return {
        "kind": "job",
        "job_id": str(job["id"]),
        "title": job["title"],
        "status": job["status"],
        "agent": job["agent"],
        "model": job["model"],
        "profile": job["profile"],
        "repo": service._short_repo(params.get("repo")),
        "steps": steps[-2:][::-1],
        "tools": len(steps),
        "elapsed_s": int((now - started).total_seconds()) if started else 0,
        "timeout_s": params.get("timeout_s"),
        "actions": actions.describe(job),
    }


def done_view(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "succeeded": sum(r["status"] == "succeeded" for r in rows),
        "failed": sum(r["status"] != "succeeded" for r in rows),
        "latest": [
            {
                "job_id": str(r["id"]),
                "title": r["title"],
                "status": r["status"],
                "repo": service._short_repo(r["repo"]),
                "finished_at": r["finished_at"],
            }
            for r in rows[:DONE_SHOWN]
        ],
        "window_hours": DONE_WINDOW_HOURS,
    }


async def summary() -> dict[str, Any]:
    needs = [c for c in await service.cards() if c["stage"] == "needs_me"]
    asking = [uuid.UUID(c["job_id"]) for c in needs if c["job_status"] == "needs_input"]
    asked = await store.questions(asking) if asking else {}
    for c in needs:
        c["question"] = asked.get(c["job_id"])
    now = datetime.now(UTC)
    working = []
    for job in await store.working_jobs():
        events = await jobs.get_recent_events(job["id"], 500)
        working.append(working_view(job, jobs_service.recent_steps(events, limit=10_000), now))
    dispatch_working, dispatch_needs = await dispatch.command_parts(now)
    working += dispatch_working
    needs += dispatch_needs
    return {
        "needs": needs,
        "working": working,
        "done": done_view(await store.finished_since(DONE_WINDOW_HOURS)),
    }
