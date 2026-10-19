"""Backlog API for the console (plans/07 section 9b): read, add, approve, dismiss, restore.

Nothing here deletes: a dismissed item stays as a record so the scout never proposes it again.
"""

import uuid
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.backlog import service, store
from app.jobs.service import JobError

router = APIRouter()

ItemType = Literal["idea", "bug", "gap", "optimization", "planned", "known_issue"]


class ItemCreate(BaseModel):
    type: ItemType
    title: str = Field(..., min_length=3, max_length=200)
    summary: str = Field("", max_length=8000)
    why: str = Field("", max_length=4000)
    repos: list[str] = Field(default_factory=list, max_length=10)
    effort: Literal["S", "M", "L"] | None = None
    impact: Literal["low", "medium", "high"] | None = None
    sources: list[str] = Field(default_factory=list, max_length=20)


class ItemUpdate(BaseModel):
    status: Literal["new", "shortlisted", "done", "dismissed"] | None = None
    type: ItemType | None = None
    note: str | None = Field(None, max_length=4000)
    title: str | None = Field(None, min_length=3, max_length=200)
    summary: str | None = Field(None, max_length=8000)
    why: str | None = Field(None, max_length=4000)
    rank: int | None = Field(None, ge=1, le=999)  # pick-up order; the weekly triage ranks ideas


class ApproveRequest(BaseModel):
    repo: str = Field(..., min_length=1, max_length=200)
    note: str = Field("", max_length=4000)
    model: str | None = None


@router.get("")
async def list_items(
    type: ItemType | None = Query(None),
    view: Literal["open", "done", "dismissed", "all"] = Query("open"),
    limit: int = Query(200, ge=1, le=500),
):
    return {"items": await store.list_items(type, view, limit)}


@router.get("/{item_id}")
async def get_item(item_id: uuid.UUID):
    item = await store.get_item(item_id)
    if item is None:
        raise HTTPException(404, "Item not found")
    return item


@router.post("", status_code=201)
async def create_item(body: ItemCreate):
    item, duplicate = await service.add_item(source="console", **body.model_dump())
    if duplicate:
        raise HTTPException(409, f"Already in the backlog: {duplicate['title']}")
    return item


@router.patch("/{item_id}")
async def update_item(item_id: uuid.UUID, body: ItemUpdate):
    fields = body.model_dump(exclude_none=True)
    if not fields:
        raise HTTPException(422, "Nothing to update")
    item = await store.update_item(item_id, **fields)
    if item is None:
        raise HTTPException(404, "Item not found")
    return item


@router.post("/{item_id}/approve")
async def approve(item_id: uuid.UUID, body: ApproveRequest):
    """Start the change now: a Claude Code job edits the repo and the bot opens a PR."""
    try:
        return await service.approve(item_id, body.repo, body.note, body.model)
    except service.BacklogError as e:
        raise HTTPException(409, str(e)) from e
    except JobError as e:
        raise HTTPException(422, str(e)) from e
