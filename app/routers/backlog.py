"""Backlog API for the console (plans/07 section 9b): read, add, approve, dismiss, restore.

Nothing here deletes: a dismissed item stays as a record so the scout never proposes it again.
"""

import re
import uuid
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, model_validator

from app.backlog import service, store
from app.board import plans, projects
from app.jobs.service import JobError

router = APIRouter()

ItemType = Literal["idea", "bug", "gap", "optimization", "planned", "known_issue", "guard"]


class ItemCreate(BaseModel):
    type: ItemType
    title: str = Field(..., min_length=3, max_length=200)
    summary: str = Field("", max_length=8000)
    why: str = Field("", max_length=4000)
    repos: list[str] = Field(default_factory=list, max_length=10)
    effort: Literal["S", "M", "L"] | None = None
    impact: Literal["low", "medium", "high"] | None = None
    sources: list[str] = Field(default_factory=list, max_length=20)
    workstream_id: uuid.UUID | None = None
    plan: str | None = Field(None, max_length=80)
    plan_section: str | None = Field(None, max_length=80)
    allow_similar: bool = False
    similar_reason: str | None = Field(None, max_length=500)

    @model_validator(mode="after")
    def _reason_for_override(self):
        if self.allow_similar and not (self.similar_reason or "").strip():
            raise ValueError("similar_reason is required when allow_similar is true")
        return self


_PR_REF = re.compile(
    r"^(?:https://github\.com/)?(?P<repo>[\w.-]+/[\w.-]+?)(?:\.git)?(?:/pull/|#)(?P<number>[1-9]\d{0,8})/?$"
)


class SessionLink(BaseModel):
    """A session building the card: a label, a branch, the PR it will merge. Null unlinks."""

    session: str | None = Field(None, min_length=1, max_length=200)
    branch: str | None = Field(None, min_length=1, max_length=255)
    pr: str | None = Field(None, max_length=300, description="PR URL, or owner/repo#number")

    @model_validator(mode="after")
    def _something_to_link(self):
        if not (self.session or self.branch or self.pr):
            raise ValueError("link needs a session, a branch or a pr")
        if self.pr and not _PR_REF.match(self.pr.strip()):
            raise ValueError("pr must be a GitHub PR URL or owner/repo#number")
        return self

    def columns(self) -> dict:
        """Only the fields the caller sent, so a later partial link keeps the earlier ones."""
        cols: dict = {}
        if "session" in self.model_fields_set:
            cols["session_label"] = self.session
        if "branch" in self.model_fields_set:
            cols["session_branch"] = self.branch
        if "pr" in self.model_fields_set:
            found = _PR_REF.match(self.pr.strip()) if self.pr else None
            cols["pr_repo"] = found["repo"] if found else None
            cols["pr_number"] = int(found["number"]) if found else None
        return cols


_NO_LINK = {"session_label": None, "session_branch": None, "pr_repo": None, "pr_number": None}


class ItemUpdate(BaseModel):
    status: Literal["new", "shortlisted", "done", "dismissed", "parked"] | None = None
    type: ItemType | None = None
    note: str | None = Field(None, max_length=4000)
    title: str | None = Field(None, min_length=3, max_length=200)
    summary: str | None = Field(None, max_length=8000)
    why: str | None = Field(None, max_length=4000)
    rank: int | None = Field(None, ge=1, le=999)  # same as /move with this position
    workstream_id: uuid.UUID | None = None  # sent as null, it takes the item off its workstream
    plan: str | None = Field(None, max_length=80)  # sent as null, it unlinks the plan
    plan_section: str | None = Field(None, max_length=80)
    link: SessionLink | None = None


class ApproveRequest(BaseModel):
    repo: str = Field(..., min_length=1, max_length=200)
    note: str = Field("", max_length=4000)
    model: str | None = None


@router.get("")
async def list_items(
    type: ItemType | None = Query(None),
    view: Literal["open", "done", "dismissed", "parked", "all"] = Query("open"),
    limit: int = Query(200, ge=1, le=500),
):
    return {"items": await store.list_items(type, view, limit)}


@router.get("/{item_id}")
async def get_item(item_id: uuid.UUID):
    item = await store.get_item(item_id)
    if item is None:
        raise HTTPException(404, "Item not found")
    return item


async def _check_workstream(ws_id: uuid.UUID | None) -> None:
    if ws_id is not None and not await projects.workstream_exists(ws_id):
        raise HTTPException(422, f"No workstream {ws_id}; list them with GET /board/projects")


async def _plan_link(slug: str | None, section: str | None) -> dict:
    if slug is None:
        return {"plan_id": None, "plan_section": None, "plan_version": None}
    try:
        plan = await plans.get_plan(slug)
    except plans.PlanError as e:
        raise HTTPException(422, str(e)) from e
    if section and section not in {s["id"] for s in plan["sections"]}:
        raise HTTPException(422, f"Plan {slug} has no section {section!r}")
    return {
        "plan_id": uuid.UUID(plan["id"]),
        "plan_section": section,
        "plan_version": plan["version"],
    }


@router.post("", status_code=201)
async def create_item(body: ItemCreate):
    await _check_workstream(body.workstream_id)
    link = await _plan_link(body.plan, body.plan_section) if body.plan else {}
    fields = body.model_dump(exclude={"plan", "plan_section", "similar_reason"}) | link
    if body.allow_similar:
        fields["note"] = f"Filed despite a similar item: {body.similar_reason.strip()}"
    # Parked items don't block: a parked idea shouldn't silently stop a different card. They are
    # reported on the response instead, so the filer learns the parked card exists.
    item, duplicate = await service.add_item(
        source="console", duplicate_statuses=store.OPEN, **fields
    )
    if duplicate:
        existing = jsonable_encoder(duplicate)
        return JSONResponse(
            status_code=409,
            content={
                "detail": (
                    f"Already in the backlog: {duplicate['title']} "
                    f"({duplicate['status']}, id {duplicate['id']})"
                ),
                "existing": existing,
            },
        )
    parked = None if body.allow_similar else await store.find_duplicate(body.title, ("parked",))
    if parked:
        return jsonable_encoder(item) | {"similar_parked": jsonable_encoder(parked)}
    return item


@router.patch("/{item_id}")
async def update_item(item_id: uuid.UUID, body: ItemUpdate):
    fields = body.model_dump(exclude_none=True, exclude={"plan", "plan_section", "link"})
    if "link" in body.model_fields_set:
        fields |= body.link.columns() if body.link else _NO_LINK
    if "plan" in body.model_fields_set:
        fields |= await _plan_link(body.plan, body.plan_section)
    if "workstream_id" in body.model_fields_set:
        await _check_workstream(body.workstream_id)
        fields["workstream_id"] = body.workstream_id
    rank = fields.pop("rank", None)
    if not fields and rank is None:
        raise HTTPException(422, "Nothing to update")
    item = await store.update_item(item_id, **fields) if fields else await store.get_item(item_id)
    if item is None:
        raise HTTPException(404, "Item not found")
    if rank is not None:
        try:
            item = await store.move_item(item_id, rank)
        except ValueError as e:
            raise HTTPException(409, str(e)) from e
    return item


class MoveRequest(BaseModel):
    # 1 = top; past the end = last; null removes it from the priority list
    position: int | None = Field(..., ge=1, le=999)


@router.post("/{item_id}/move")
async def move(item_id: uuid.UUID, body: MoveRequest):
    """Set an open item's priority: insert at `position` (others shift), or unrank with null."""
    try:
        item = await store.move_item(item_id, body.position)
    except ValueError as e:
        raise HTTPException(409, str(e)) from e
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
