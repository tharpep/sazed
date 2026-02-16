"""Conversation history endpoints."""

from fastapi import APIRouter, HTTPException

from app.agent.loop import get_session, list_sessions
from app.agent.session import process_session

router = APIRouter()


@router.get("")
async def list_conversations():
    return {"conversations": list_sessions()}


@router.get("/{session_id}")
async def get_conversation(session_id: str):
    messages = get_session(session_id)
    if messages is None:
        raise HTTPException(404, "Session not found")
    return {"session_id": session_id, "messages": messages, "message_count": len(messages)}


@router.post("/{session_id}/process")
async def trigger_process_session(session_id: str):
    messages = get_session(session_id)
    if messages is None:
        raise HTTPException(404, "Session not found")
    if not messages:
        raise HTTPException(400, "Session has no messages to process")
    return await process_session(session_id, messages)
