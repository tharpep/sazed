"""Events from outside (plans/16): the gateway verifies and forwards them; app.intake decides."""

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from app.intake import policy, service, store

router = APIRouter()


class GithubEventIn(BaseModel):
    event: str = Field(..., max_length=50)
    action: str = Field("", max_length=50)
    repo: str = Field(..., min_length=3, max_length=200)
    number: int = Field(..., ge=1)
    is_pr: bool = False
    sender: str = Field("", max_length=100)
    title: str = Field("", max_length=500)
    body: str = Field("", max_length=8000)
    branch: str | None = Field(None, max_length=255)
    draft: bool = False
    url: str = Field("", max_length=500)
    path: str | None = Field(None, max_length=500)
    line: int | None = None


@router.post("/github")
async def github_event(body: GithubEventIn):
    return await service.route_github(policy.GithubEvent(**body.model_dump()))


@router.get("/triggers")
async def recent_triggers(limit: int = Query(50, ge=1, le=200)):
    return {"triggers": await store.recent(limit)}
