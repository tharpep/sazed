"""Dispatch plans, rows, steps and heartbeats from sazed-worker (migrations 024, 027, 034)."""

import math
import time
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError

from app.board import dispatch
from app.dependencies import ShapeV2
from app.jobs import dispatch_jobs, service
from app.learning import service as learning

router = APIRouter()

# sazed-worker's plan.DEFAULT_PROFILE: what a task that names no profile runs as.
DEFAULT_PROFILE = "implement"

# Raw words; dispatch_state (migration 035) maps each onto the run states. Adding one here needs
# a branch there (pinned by tests/test_dispatch_state.py).
RowState = Literal[
    "running",
    "done",
    "waiting",
    "failed",
    "landed",
    "conflict",
    "blocked",
    "cleaned",
    "timed_out",
]


class TaskIn(BaseModel):
    id: str
    goal: str = ""
    owns: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    profile: str = ""
    model: str = ""


class PlanIn(BaseModel):
    plan: str = Field(..., min_length=1, max_length=200)
    repo: str
    base: str
    host: str
    tasks: list[TaskIn]
    project: str | None = Field(None, max_length=60)
    board_plan: str | None = Field(None, max_length=60)


class RowIn(BaseModel):
    plan: str = Field(..., min_length=1, max_length=200)
    task: str = Field(..., min_length=1, max_length=200)
    ts: float
    state: RowState
    line: str = ""
    ref: str | None = None
    cost_usd: float | None = None
    session: str | None = None
    meta: dict = Field(default_factory=dict)


class HeartbeatIn(BaseModel):
    plan: str = Field(..., min_length=1, max_length=200)
    task: str = Field(..., min_length=1, max_length=200)


class StepIn(BaseModel):
    ts: float
    seq: int
    kind: Literal["tool", "result", "text"]
    tool: str | None = None
    text: str = ""
    error: bool = False


class StepsIn(BaseModel):
    plan: str = Field(..., min_length=1, max_length=200)
    task: str = Field(..., min_length=1, max_length=200)
    steps: list[StepIn] = Field(..., min_length=1, max_length=200)


def _task_metadata(raw: dict) -> dict:
    """A plan.json task as the mirror sends it; a run's own mirror later adds its resolved model."""
    known = {k: raw[k] for k in TaskIn.model_fields if raw.get(k) is not None}
    return {"profile": DEFAULT_PROFILE} | known


async def _submitted(request: Request) -> bytes:
    raw = bytearray()
    async for chunk in request.stream():
        raw += chunk
        if len(raw) > dispatch_jobs.MAX_BODY:
            raise service.JobError("the submitted plan is larger than 1 MiB")
    return bytes(raw)


@router.post("/plans/{plan}/submit", status_code=201)
async def submit_plan(plan: str, request: Request):
    try:
        body = dispatch_jobs.parse(await _submitted(request))
        raw = body.plan.get("tasks")
        mirrored = PlanIn(
            plan=plan,
            repo=body.repo,
            base=body.base,
            host="",
            tasks=[
                _task_metadata(t)
                for t in (raw if isinstance(raw, list) else [])
                if isinstance(t, dict)
            ],
            project=body.plan.get("project"),
            board_plan=body.plan.get("board_plan"),
        )
        job = await dispatch_jobs.submit(
            plan,
            body,
            [t.model_dump() for t in mirrored.tasks],
            mirrored.project,
            mirrored.board_plan,
        )
    except ValidationError as e:
        first = e.errors()[0]
        where = ".".join(str(p) for p in first["loc"])
        raise HTTPException(422, f"plan {where}: {first['msg']}") from e
    except dispatch_jobs.PlanConflict as e:
        raise HTTPException(409, str(e)) from e
    except service.GatewayUnreachable as e:
        raise HTTPException(503, str(e)) from e
    except service.JobError as e:
        raise HTTPException(422, str(e)) from e
    return {
        "job_id": job["id"],
        "plan": plan,
        "plan_hash": job["params"]["plan_hash"],
        "status": job["status"],
    }


@router.post("/plans")
async def post_plan(body: PlanIn):
    tasks = [t.model_dump() for t in body.tasks]
    await dispatch.upsert_plan(
        body.plan, body.repo, body.base, body.host, tasks, body.project, body.board_plan
    )
    return {"plan": body.plan, "tasks": len(tasks)}


TS_AHEAD_S = 3600
INT4 = 2**31


def _refuse_out_of_range(name: str, value: float | None, most: float = math.inf) -> None:
    # Not a Field constraint: FastAPI answers one with the input, and Infinity isn't JSON (500).
    if value is not None and not (math.isfinite(value) and 0 <= value <= most):
        raise HTTPException(422, f"{name} is out of range")


def _check_ts(ts: float) -> None:
    _refuse_out_of_range("ts", ts, time.time() + TS_AHEAD_S)


@router.post("/rows", status_code=201)
async def post_row(body: RowIn, background: BackgroundTasks):
    _check_ts(body.ts)
    _refuse_out_of_range("cost_usd", body.cost_usd)
    await dispatch.insert_row(**body.model_dump())
    background.add_task(learning.record_dispatch, body.plan, body.task, body.state, body.meta)
    return {"ok": True}


@router.post("/heartbeat")
async def post_heartbeat(body: HeartbeatIn):
    await dispatch.heartbeat(body.plan, body.task)
    return {"ok": True}


@router.post("/steps", status_code=201)
async def post_steps(body: StepsIn):
    for step in body.steps:
        _check_ts(step.ts)
        if not -INT4 <= step.seq < INT4:
            raise HTTPException(422, "seq is out of range")
    await dispatch.insert_steps(body.plan, body.task, [s.model_dump() for s in body.steps])
    return {"ok": True, "stored": len(body.steps)}


@router.get("/plans")
async def list_plans(v2: ShapeV2):
    return await dispatch.list_plans(v2)


@router.get("/plans/{plan}")
async def get_plan(plan: str, v2: ShapeV2):
    found = await dispatch.get_plan(plan, v2)
    if not found:
        raise HTTPException(404, "Plan not found")
    return {**found, "now": datetime.now(UTC).isoformat()}
