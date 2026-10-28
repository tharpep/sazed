"""Approvals (plans/12 phase 1): steps waiting on Pryce. Approving an agent's pause step resumes
its run; declining ends it as skipped. The console's approvals inbox comes with plans/13."""

import uuid
from typing import Literal

from fastapi import APIRouter, HTTPException, Query

from app.agents import approvals
from app.automations import runner, store

router = APIRouter()


@router.get("")
async def list_approvals(status: Literal["pending", "approved", "denied", "expired"] = Query(
    "pending"
)):
    return {"approvals": await approvals.list_approvals(status)}


async def _decide(approval_id: uuid.UUID, verdict: str) -> dict:
    row = await approvals.decide(approval_id, verdict)
    if row is None:
        raise HTTPException(409, "not waiting for an answer (already decided, expired or unknown)")
    return row


@router.post("/{approval_id}/approve")
async def approve(approval_id: uuid.UUID):
    row = await _decide(approval_id, "approved")
    run_id = await runner.trigger_resume(row) if row["kind"] == "agent_step" else None
    return {"id": row["id"], "status": row["status"], "resumed_run": run_id}


@router.post("/{approval_id}/deny")
async def deny(approval_id: uuid.UUID):
    row = await _decide(approval_id, "denied")
    if row["kind"] == "agent_step":
        run_id = uuid.UUID(row["payload"]["run_id"])
        steps = row["payload"].get("steps") or []
        if steps:
            steps[-1]["status"] = "denied"
            await store.set_steps(run_id, steps)
        await store.finish_run(run_id, "skipped", output=f"You declined: {row['title']}")
    return {"id": row["id"], "status": row["status"]}
