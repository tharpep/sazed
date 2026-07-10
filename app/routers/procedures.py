"""Procedural memory endpoints."""

from fastapi import APIRouter, HTTPException, Query

from app.agent.procedures import (
    activate_procedure,
    archive_procedure,
    delete_procedure,
    list_procedures,
)

router = APIRouter()


@router.get("")
async def get_procedures(status: str | None = Query(default=None)):
    procedures = await list_procedures(status)
    return {"procedures": procedures, "count": len(procedures)}


@router.post("/{procedure_id}/activate")
async def activate(procedure_id: str):
    if not await activate_procedure(procedure_id):
        raise HTTPException(404, "Procedure not found")
    return {"activated": procedure_id}


@router.post("/{procedure_id}/archive")
async def archive(procedure_id: str):
    if not await archive_procedure(procedure_id):
        raise HTTPException(404, "Procedure not found")
    return {"archived": procedure_id}


@router.delete("/{procedure_id}")
async def delete(procedure_id: str):
    if not await delete_procedure(procedure_id):
        raise HTTPException(404, "Procedure not found")
    return {"deleted": procedure_id}
