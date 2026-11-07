"""Dispatch plans, rows and steps from sazed-worker (migrations 024, 027)."""

from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.board import dispatch

router = APIRouter()

RowState = Literal[
    "running", "done", "waiting", "failed", "landed", "conflict", "blocked", "cleaned"
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


@router.post("/plans")
async def post_plan(body: PlanIn):
    tasks = [t.model_dump() for t in body.tasks]
    await dispatch.upsert_plan(
        body.plan, body.repo, body.base, body.host, tasks, body.project, body.board_plan
    )
    return {"plan": body.plan, "tasks": len(tasks)}


@router.post("/rows", status_code=201)
async def post_row(body: RowIn):
    await dispatch.insert_row(**body.model_dump())
    return {"ok": True}


@router.post("/steps", status_code=201)
async def post_steps(body: StepsIn):
    await dispatch.insert_steps(body.plan, body.task, [s.model_dump() for s in body.steps])
    return {"ok": True, "stored": len(body.steps)}


@router.get("/plans")
async def list_plans():
    return await dispatch.list_plans()


@router.get("/plans/{plan}")
async def get_plan(plan: str):
    found = await dispatch.get_plan(plan)
    if not found:
        raise HTTPException(404, "Plan not found")
    return {**found, "now": datetime.now(UTC).isoformat()}
