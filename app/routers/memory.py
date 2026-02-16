"""Structured memory (agent_memory) endpoints."""

from fastapi import APIRouter, HTTPException

router = APIRouter()


@router.get("")
async def list_memory():
    raise HTTPException(501, "Not implemented yet")


@router.put("")
async def upsert_memory():
    raise HTTPException(501, "Not implemented yet")


@router.delete("/{memory_id}")
async def delete_memory(memory_id: str):
    raise HTTPException(501, "Not implemented yet")
