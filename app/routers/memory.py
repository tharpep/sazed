"""Structured memory (agent_memory) endpoints."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.agent.memory import delete_fact, load_memory, upsert_fact

router = APIRouter()


class UpsertMemoryRequest(BaseModel):
    fact_type: str
    key: str
    value: str
    confidence: float = 1.0


@router.get("")
async def list_memory():
    facts = load_memory()
    return {"facts": facts, "count": len(facts)}


@router.put("")
async def upsert_memory(body: UpsertMemoryRequest):
    return upsert_fact(
        fact_type=body.fact_type,
        key=body.key,
        value=body.value,
        confidence=body.confidence,
        source="api",
    )


@router.delete("/{memory_id}")
async def delete_memory(memory_id: str):
    if not delete_fact(memory_id):
        raise HTTPException(404, "Fact not found")
    return {"deleted": memory_id}
