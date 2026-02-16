"""Chat endpoint — main agent interface."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter()


class ChatRequest(BaseModel):
    session_id: str | None = None
    message: str


class ChatResponse(BaseModel):
    session_id: str
    response: str


@router.post("", response_model=ChatResponse)
async def chat(body: ChatRequest):
    raise HTTPException(501, "Not implemented yet")
