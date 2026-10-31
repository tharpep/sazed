import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.capture import service, store

router = APIRouter()


class CaptureRequest(BaseModel):
    content: str = Field(..., min_length=1, max_length=100_000)
    source: str = Field("shortcut", max_length=50)


class CaptureResponse(BaseModel):
    id: str
    content: str
    source: str
    status: str
    routed_to: list[dict[str, Any]]
    created_at: str
    processed_at: str | None = None
    error: str | None = None


def _format_capture(c: dict[str, Any]) -> dict[str, Any]:
    created = c["created_at"]
    created_str = created.isoformat() if hasattr(created, "isoformat") else str(created)
    processed = c.get("processed_at")
    processed_str = (
        processed.isoformat()
        if processed and hasattr(processed, "isoformat")
        else str(processed)
        if processed
        else None
    )
    return {
        "id": str(c["id"]),
        "content": c["content"],
        "source": c["source"],
        "status": c["status"],
        "routed_to": c.get("routed_to") or [],
        "created_at": created_str,
        "processed_at": processed_str,
        "error": c.get("error"),
    }


@router.post("", response_model=CaptureResponse, status_code=201)
async def create_capture(body: CaptureRequest):
    capture = await service.ingest_capture(content=body.content, source=body.source)
    return _format_capture(capture)


@router.get("", response_model=list[CaptureResponse])
async def list_captures(
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    rows = await store.list_captures(limit=limit, offset=offset)
    return [_format_capture(r) for r in rows]


@router.get("/{capture_id}", response_model=CaptureResponse)
async def get_capture(capture_id: uuid.UUID):
    row = await store.get_capture(capture_id)
    if not row:
        raise HTTPException(status_code=404, detail="Capture not found")
    return _format_capture(row)


@router.post("/{capture_id}/reprocess", response_model=CaptureResponse)
async def reprocess_capture(capture_id: uuid.UUID):
    try:
        updated = await service.process_capture(capture_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return _format_capture(updated)
