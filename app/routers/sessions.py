"""Claude Code sessions reporting through hooks (migration 039, slice 3)."""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.board import service as board_service
from app.sessions import service

router = APIRouter()

# 200 events of about 1 KB each fit with room to spare; text is cut to 300 characters anyway.
MAX_BODY_BYTES = 1_000_000


async def _capped(request: Request) -> None:
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        raise HTTPException(413, f"Body over {MAX_BODY_BYTES} bytes")
    if len(await request.body()) > MAX_BODY_BYTES:
        raise HTTPException(413, f"Body over {MAX_BODY_BYTES} bytes")


class EventsIn(BaseModel):
    # Each event is validated on its own in service.parse_batch, so a malformed one, even one
    # that is not an object, is rejected alone instead of failing the batch.
    events: list[Any] = Field(..., min_length=1, max_length=200)


@router.post("/events", dependencies=[Depends(_capped)])
async def post_events(body: EventsIn):
    return await service.ingest(body.events)


@router.get("")
async def list_sessions():
    return await service.list_sessions(await board_service.read(all_done=True))


@router.get("/{session_id}")
async def get_session(session_id: str):
    found = await service.detail(session_id, await board_service.cards(all_done=True))
    if found is None:
        raise HTTPException(404, "Session not found")
    return found
