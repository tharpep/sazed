"""Conversation history endpoints."""

from fastapi import APIRouter, HTTPException

router = APIRouter()


@router.get("")
async def list_conversations():
    raise HTTPException(501, "Not implemented yet")


@router.get("/{session_id}")
async def get_conversation(session_id: str):
    raise HTTPException(501, "Not implemented yet")


@router.post("/{session_id}/process")
async def process_session(session_id: str):
    raise HTTPException(501, "Not implemented yet")
