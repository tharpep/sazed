"""Chat endpoint — main agent interface."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.agent.loop import run_turn

router = APIRouter()


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

    session_id, response_text = await run_turn(body.session_id, body.message)
    return ChatResponse(session_id=session_id, response=response_text)
