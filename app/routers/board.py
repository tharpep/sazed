"""The board (plans/13): every card in one read, plus the projects and workstreams above them."""

import uuid
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.board import projects, service

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
    return found


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


@router.get("/{card_id}")
async def get_card(card_id: str):
    card = await service.card(card_id)
    if card is None:
        raise HTTPException(404, "Card not found")
    return card
