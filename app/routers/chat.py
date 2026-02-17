"""Chat endpoint — main agent interface."""

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.agent.loop import run_turn

router = APIRouter()
logger = logging.getLogger(__name__)


class ChatRequest(BaseModel):
    session_id: str | None = None
    message: str


class ChatResponse(BaseModel):
    session_id: str
    response: str


@router.post("", response_model=ChatResponse)
async def chat(body: ChatRequest):
    if not body.message.strip():
        raise HTTPException(400, "Message cannot be empty")

    logger.debug(f"POST /chat: session={body.session_id}, message='{body.message[:120]}'")
    session_id, response_text = await run_turn(body.session_id, body.message)
    logger.debug(f"POST /chat: done, session={session_id}, response='{response_text[:120]}'")
    return ChatResponse(session_id=session_id, response=response_text)
