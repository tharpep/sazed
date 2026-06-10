"""Structured memory (agent_memory) endpoints."""

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.agent.memory import (
    delete_fact,
    load_memory,
    load_stale_memory,
    memory_history,
    upsert_fact,
)

router = APIRouter()


class UpsertMemoryRequest(BaseModel):
    fact_type: str
    key: str
    value: str
    confidence: float = 1.0


@router.get("")
async def list_memory():
    facts = await load_memory()
    return {"facts": facts, "count": len(facts)}


@router.get("/stale")
async def get_stale_memory(days: int = Query(default=30, ge=1)):
    facts = await load_stale_memory(days)
    return {"facts": facts, "count": len(facts), "days": days}


@router.get("/{fact_type}/{key}/history")
async def get_memory_history(fact_type: str, key: str):
    rows = await memory_history(fact_type, key)
    return {"fact_type": fact_type, "key": key, "history": rows, "count": len(rows)}


@router.put("")
async def upsert_memory(body: UpsertMemoryRequest):
    return await upsert_fact(
        fact_type=body.fact_type,
        key=body.key,
        value=body.value,
        confidence=body.confidence,
        source="api",
    )


@router.delete("/{memory_id}")
async def delete_memory(memory_id: str):
    if not await delete_fact(memory_id):
        raise HTTPException(404, "Fact not found")
    return {"deleted": memory_id}
