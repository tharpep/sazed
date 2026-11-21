"""Claude Code sessions reporting through hooks (migration 039, slice 3)."""

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.board import service as board_service
from app.sessions import service

router = APIRouter()


class EventsIn(BaseModel):
    # Each event is validated on its own in service.parse_batch, so a malformed one is rejected
    # alone instead of failing the batch.
    events: list[dict[str, Any]] = Field(..., min_length=1, max_length=200)


@router.post("/events")
async def post_events(body: EventsIn):
    return await service.ingest(body.events)


@router.get("")
async def list_sessions():
    return await service.list_sessions(await board_service.cards(all_done=True))


@router.get("/{session_id}")
async def get_session(session_id: str):
    found = await service.detail(session_id, await board_service.cards(all_done=True))
    if found is None:
        raise HTTPException(404, "Session not found")
    return found
