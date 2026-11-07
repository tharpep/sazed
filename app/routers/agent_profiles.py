"""Agent profiles for dispatch: model, effort, budget, timeout and write access (migration 028)."""

from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Query
from pydantic import BaseModel, Field

from app.board import profiles

router = APIRouter()

AUTHOR = r"^(pryce|agent:[\w.:-]{1,60}|dispatch:[\w.:-]{1,60})$"
Name = Annotated[str, Path(pattern=r"^[a-z][a-z0-9-]{1,30}$")]


class ProfileIn(BaseModel):
    model: str = Field(..., pattern=r"^(haiku|sonnet|opus|fable|claude-[\w.\-\[\]]+)$")
    effort: str = Field(..., pattern=r"^(low|medium|high|xhigh|max)$")
    budget_usd: float = Field(..., ge=0.05, le=50)
    timeout_s: int = Field(..., ge=60, le=7200)
    read_only: bool
    evidence_required: bool = False
    description: str = Field("", max_length=300)
    author: str = Field(..., pattern=AUTHOR)
    reason: str = Field(..., min_length=3, max_length=300)
    expected_version: int | None = Field(None, ge=1)


def _http(e: profiles.ProfileError) -> HTTPException:
    return HTTPException(e.status, str(e))


@router.get("/profiles")
async def list_profiles():
    return await profiles.list_profiles()


@router.get("/profiles/{name}")
async def get_profile(name: Name):
    try:
        return await profiles.get(name)
    except profiles.ProfileError as e:
        raise _http(e) from e


@router.put("/profiles/{name}")
async def put_profile(name: Name, body: ProfileIn):
    try:
        return await profiles.save(name, **body.model_dump())
    except profiles.ProfileError as e:
        raise _http(e) from e


@router.delete("/profiles/{name}")
async def delete_profile(
    name: Name,
    author: str = Query(..., pattern=AUTHOR),
    reason: str = Query(..., min_length=3, max_length=300),
):
    try:
        return await profiles.delete(name, author=author, reason=reason)
    except profiles.ProfileError as e:
        raise _http(e) from e
