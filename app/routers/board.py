"""The board (plans/13): every card in one read, plus the projects and workstreams above them."""

import uuid
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.board import actions, command, dispatch, plans, projects, service

router = APIRouter()

ProjectState = Literal["active", "paused", "done", "dropped"]
WorkstreamState = Literal["open", "done", "dropped"]


class ProjectCreate(BaseModel):
    slug: str = Field(..., pattern=r"^[a-z0-9][a-z0-9-]{1,40}$")
    title: str = Field(..., min_length=2, max_length=120)
    summary: str = Field("", max_length=400)
    state: ProjectState = "active"
    rank: int | None = Field(None, ge=1, le=999)
    repos: list[str] = Field(default_factory=list, max_length=10)
    plan: str | None = Field(None, max_length=200)


class ProjectUpdate(BaseModel):
    title: str | None = Field(None, min_length=2, max_length=120)
    summary: str | None = Field(None, max_length=400)
    state: ProjectState | None = None
    rank: int | None = Field(None, ge=1, le=999)
    repos: list[str] | None = Field(None, max_length=10)
    plan: str | None = Field(None, max_length=200)
    latest_update: str | None = Field(None, max_length=1200)


class WorkstreamCreate(BaseModel):
    title: str = Field(..., min_length=2, max_length=120)
    summary: str = Field("", max_length=400)
    position: int | None = Field(None, ge=1, le=999)
    state: WorkstreamState = "open"
    depends_on: list[uuid.UUID] = Field(default_factory=list, max_length=20)


class WorkstreamUpdate(BaseModel):
    title: str | None = Field(None, min_length=2, max_length=120)
    summary: str | None = Field(None, max_length=400)
    position: int | None = Field(None, ge=1, le=999)
    state: WorkstreamState | None = None
    depends_on: list[uuid.UUID] | None = Field(None, max_length=20)


def _fields(body: BaseModel) -> dict:
    fields = body.model_dump(exclude_none=True)
    if not fields:
        raise HTTPException(422, "Nothing to update")
    return fields


def _error(e: projects.ProjectError) -> HTTPException:
    missing = str(e).startswith("no ")
    return HTTPException(404 if missing else 422, str(e))


@router.get("")
async def get_board():
    return service.board(await service.cards())


# Registered before /{card_id}, which would otherwise take these paths.
@router.get("/projects")
async def list_projects(all: bool = Query(False, description="Include done and dropped")):
    return {"projects": await projects.list_projects(include_closed=all)}


@router.post("/projects", status_code=201)
async def create_project(body: ProjectCreate):
    try:
        return await projects.create_project(**body.model_dump(exclude_none=True))
    except projects.ProjectError as e:
        raise HTTPException(409, str(e)) from e


@router.get("/projects/{slug}")
async def get_project(slug: str):
    found = await projects.get_project(slug)
    if found is None:
        raise HTTPException(404, "Project not found")
    return {**found, "dispatch_runs": await dispatch.runs_for_project(slug)}


@router.patch("/projects/{slug}")
async def update_project(slug: str, body: ProjectUpdate):
    try:
        return await projects.update_project(slug, **_fields(body))
    except projects.ProjectError as e:
        raise _error(e) from e


@router.post("/projects/{slug}/workstreams", status_code=201)
async def create_workstream(slug: str, body: WorkstreamCreate):
    try:
        return await projects.create_workstream(slug, **body.model_dump(exclude_none=True))
    except projects.ProjectError as e:
        raise _error(e) from e


@router.patch("/workstreams/{ws_id}")
async def update_workstream(ws_id: uuid.UUID, body: WorkstreamUpdate):
    try:
        return await projects.update_workstream(ws_id, **_fields(body))
    except projects.ProjectError as e:
        raise _error(e) from e


PlanState = Literal["draft", "active", "done", "superseded"]
Author = Field(..., pattern=r"^(pryce|agent:[\w.:-]{1,60}|dispatch:[\w.:-]{1,60})$")


class PlanCreate(BaseModel):
    slug: str = Field(..., pattern=r"^[a-z0-9][a-z0-9-]{1,60}$")
    title: str = Field(..., min_length=2, max_length=160)
    body: str = Field(..., min_length=1, max_length=200_000)
    author: str = Author
    reason: str = Field(..., min_length=3, max_length=300)
    project: str | None = Field(None, max_length=60)
    workstream_id: uuid.UUID | None = None
    state: PlanState = "draft"


class PlanSave(BaseModel):
    body: str = Field(..., min_length=1, max_length=200_000)
    author: str = Author
    reason: str = Field(..., min_length=3, max_length=300)
    expected_version: int | None = Field(None, ge=1)


class PlanUpdate(BaseModel):
    title: str | None = Field(None, min_length=2, max_length=160)
    state: PlanState | None = None
    project: str | None = Field(None, max_length=60)
    workstream_id: uuid.UUID | None = None


def _plan_error(e: plans.PlanError) -> HTTPException:
    text = str(e)
    if text.startswith("no "):
        return HTTPException(404, text)
    if "moved to" in text or "landed first" in text or "already exists" in text:
        return HTTPException(409, text)
    return HTTPException(422, text)


@router.get("/plans")
async def list_plans(project: str | None = None, state: PlanState | None = None):
    return {"plans": await plans.list_plans(project, state)}


@router.post("/plans", status_code=201)
async def create_plan(body: PlanCreate):
    try:
        return await plans.create(**body.model_dump())
    except plans.PlanError as e:
        raise _plan_error(e) from e


@router.get("/plans/{slug}")
async def get_plan(slug: str, version: int | None = Query(None, ge=1)):
    try:
        return await plans.get_plan(slug, version)
    except plans.PlanError as e:
        raise _plan_error(e) from e


@router.post("/plans/{slug}/versions", status_code=201)
async def save_plan(slug: str, body: PlanSave):
    try:
        return await plans.save(slug, **body.model_dump())
    except plans.PlanError as e:
        raise _plan_error(e) from e


@router.get("/plans/{slug}/versions")
async def plan_versions(slug: str):
    try:
        return {"versions": await plans.versions(slug)}
    except plans.PlanError as e:
        raise _plan_error(e) from e


@router.get("/plans/{slug}/diff")
async def plan_diff(slug: str, a: int = Query(..., ge=1), b: int = Query(..., ge=1)):
    try:
        return await plans.diff(slug, a, b)
    except plans.PlanError as e:
        raise _plan_error(e) from e


@router.patch("/plans/{slug}")
async def update_plan(slug: str, body: PlanUpdate):
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(422, "Nothing to update")
    try:
        return await plans.update(slug, **fields)
    except plans.PlanError as e:
        raise _plan_error(e) from e


@router.get("/command")
async def get_command():
    return await command.summary()


class CardAction(BaseModel):
    text: str = Field("", max_length=8000)


@router.post("/{card_id}/actions/{action}")
async def run_card_action(card_id: str, action: str, body: CardAction | None = None):
    kind, _, raw_id = card_id.partition(":")
    if kind == "job":
        job_id = raw_id
    else:
        found = await service.card(card_id)
        if found is None or not found["job_id"]:
            raise HTTPException(404, "No card with a job behind it")
        job_id = found["job_id"]
    try:
        job_uuid = uuid.UUID(job_id)
    except ValueError as e:
        raise HTTPException(404, "No such job") from e
    try:
        return await actions.run(job_uuid, action, (body or CardAction()).text)
    except actions.ActionError as e:
        raise HTTPException(409, str(e)) from e


@router.get("/{card_id}")
async def get_card(card_id: str):
    card = await service.card(card_id)
    if card is None:
        raise HTTPException(404, "Card not found")
    return card
