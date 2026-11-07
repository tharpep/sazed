"""Claude usage API: a key-gated batch ingest for the pusher, and API-key reads for the console."""

import hmac
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator

from app.claude_usage import stats, store
from app.config import settings

MACHINE_PATTERN = r"^[A-Za-z0-9._-]{1,64}$"

Text = Annotated[str, Field(max_length=500)]
Detail = Annotated[str | None, Field(max_length=2000)]


def verify_usage_key(request: Request) -> None:
    if not settings.claude_usage_key:
        raise HTTPException(503, "Claude usage ingest disabled (CLAUDE_USAGE_KEY not set)")
    key = request.headers.get("X-Claude-Usage-Key") or ""
    if not hmac.compare_digest(key, settings.claude_usage_key):
        raise HTTPException(401, "Invalid or missing Claude usage key")


ingest_router = APIRouter(dependencies=[Depends(verify_usage_key)])
router = APIRouter()


class SessionRow(BaseModel):
    session_id: Text
    project: Detail = None
    title: Detail = None
    agent_name: Detail = None
    version: Detail = None
    first_ts: datetime
    last_ts: datetime


class PromptRow(BaseModel):
    uuid: Text
    session_id: Text
    ts: datetime
    chars: int = Field(..., ge=0)
    source: Detail = None
    permission_mode: Detail = None
    confused: bool


class ResponseRow(BaseModel):
    uuid: Text
    session_id: Text
    ts: datetime
    model: Detail = None
    effort: Detail = None
    output_style: Detail = None
    input_tokens: int = Field(..., ge=0)
    output_tokens: int = Field(..., ge=0)
    cache_read_tokens: int = Field(..., ge=0)
    cache_write_tokens: int = Field(..., ge=0)
    sidechain: bool
    cache_write_1h_tokens: int = Field(0, ge=0)
    speed: Detail = None
    inference_geo: Detail = None
    web_search_requests: int = Field(0, ge=0)


class ToolCallRow(BaseModel):
    tool_use_id: Text
    session_id: Text
    ts: datetime
    name: Text
    detail: Detail = None
    writes_files: bool
    sidechain: bool


class EventRow(BaseModel):
    uuid: Text
    session_id: Text
    ts: datetime
    kind: Literal["compact", "interrupt", "rejection"]


class SlashCommandRow(BaseModel):
    ts: datetime
    project: Detail
    command: Text

    @field_validator("ts", mode="before")
    @classmethod
    def _epoch_millis(cls, value):
        if isinstance(value, str) and value.isdigit():
            return datetime.fromtimestamp(int(value) / 1000, UTC)
        return value


LISTS = ("sessions", "prompts", "responses", "tool_calls", "events", "slash_commands")


class BatchRequest(BaseModel):
    machine: str = Field(..., pattern=MACHINE_PATTERN)
    sessions: list[SessionRow] = Field(default_factory=list)
    prompts: list[PromptRow] = Field(default_factory=list)
    responses: list[ResponseRow] = Field(default_factory=list)
    tool_calls: list[ToolCallRow] = Field(default_factory=list)
    events: list[EventRow] = Field(default_factory=list)
    slash_commands: list[SlashCommandRow] = Field(default_factory=list)


@ingest_router.post("/batch")
async def ingest_batch(body: BatchRequest):
    batch = {name: [row.model_dump() for row in getattr(body, name)] for name in LISTS}
    if sum(len(rows) for rows in batch.values()) > store.MAX_ROWS:
        raise HTTPException(413, f"At most {store.MAX_ROWS} rows per batch")
    return {"accepted": await store.ingest(body.machine, batch)}


def _window(days: int) -> tuple[datetime, datetime]:
    now = datetime.now(UTC).replace(microsecond=0)
    return now - timedelta(days=days), now


@router.get("/overview")
async def overview(
    days: int = Query(30, ge=1, le=365),
    machine: str | None = Query(None, pattern=MACHINE_PATTERN),
):
    frm, to = _window(days)
    return stats.build_overview(await store.overview_rows(frm, to, machine), frm, to)


@router.get("/sessions")
async def sessions(
    days: int = Query(30, ge=1, le=365),
    machine: str | None = Query(None, pattern=MACHINE_PATTERN),
    min_prompts: int = Query(1, ge=1),
):
    frm, to = _window(days)
    return stats.build_sessions(await store.session_rows(frm, to, machine, min_prompts))
