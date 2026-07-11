"""Chat endpoint — main agent interface."""

import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.agent.loop import run_turn, run_turn_stream, truncate_session_from_user_message

router = APIRouter()
logger = logging.getLogger(__name__)


class UserLocation(BaseModel):
    latitude: float
    longitude: float
    accuracy: float | None = None


class ChatRequest(BaseModel):
    session_id: str | None = None
    message: str
    mode: str = "chat"
    timezone: str | None = None
    location: UserLocation | None = None
    session_type: str = "chat"


class ChatResponse(BaseModel):
    session_id: str
    response: str


@router.post("", response_model=ChatResponse)
async def chat(body: ChatRequest):
    if not body.message.strip():
        raise HTTPException(400, "Message cannot be empty")

    logger.debug(f"POST /chat: session={body.session_id}, message='{body.message[:120]}'")
    session_id, response_text = await run_turn(
        body.session_id, body.message, body.mode, body.timezone, body.location,
        session_type=body.session_type,
    )
    logger.debug(f"POST /chat: done, session={session_id}, response='{response_text[:120]}'")
    return ChatResponse(session_id=session_id, response=response_text)


@router.post("/stream")
async def chat_stream(body: ChatRequest, request: Request):
    if not body.message.strip():
        raise HTTPException(400, "Message cannot be empty")

    logger.debug(f"POST /chat/stream: session={body.session_id}, message='{body.message[:120]}'")
    return StreamingResponse(
        run_turn_stream(
            body.session_id, body.message, body.mode, body.timezone, body.location,
            session_type=body.session_type, request=request,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


class EditMessageRequest(BaseModel):
    session_id: str
    message_index: int
    message: str
    mode: str = "chat"
    timezone: str | None = None
    location: UserLocation | None = None
    session_type: str = "chat"


@router.post("/edit", response_model=ChatResponse)
async def edit_message(body: EditMessageRequest):
    """Replace a past user message and everything after it, then re-run the turn.

    message_index is 0-based, counting only real user-authored text messages in the
    session (see truncate_session_from_user_message for the exact rule).
    """
    if not body.message.strip():
        raise HTTPException(400, "Message cannot be empty")

    try:
        await truncate_session_from_user_message(body.session_id, body.message_index)
    except ValueError as e:
        raise HTTPException(404, str(e))

    session_id, response_text = await run_turn(
        body.session_id, body.message, body.mode, body.timezone, body.location,
        session_type=body.session_type,
    )
    return ChatResponse(session_id=session_id, response=response_text)


@router.post("/edit/stream")
async def edit_message_stream(body: EditMessageRequest, request: Request):
    """Streaming variant of POST /chat/edit."""
    if not body.message.strip():
        raise HTTPException(400, "Message cannot be empty")

    try:
        await truncate_session_from_user_message(body.session_id, body.message_index)
    except ValueError as e:
        raise HTTPException(404, str(e))

    return StreamingResponse(
        run_turn_stream(
            body.session_id, body.message, body.mode, body.timezone, body.location,
            session_type=body.session_type, request=request,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
