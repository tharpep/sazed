"""Audit log — action history for all tool calls."""

import uuid

from fastapi import APIRouter, Query

from app.db import get_pool

router = APIRouter()


@router.get("/actions")
async def list_action_logs(
    session_id: str | None = Query(None),
    status: str | None = Query(None),
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
):
    pool = get_pool()
    filters: list[str] = []
    params: list = []

    if session_id:
        params.append(uuid.UUID(session_id))
        filters.append(f"session_id = ${len(params)}")
    if status:
        params.append(status)
        filters.append(f"status = ${len(params)}")

    where = f"WHERE {' AND '.join(filters)}" if filters else ""
    params += [limit, offset]

    rows = await pool.fetch(
        f"""
        SELECT id, session_id, timestamp, tool_name, input, output,
               status, error_message, duration_ms
        FROM action_logs {where}
        ORDER BY timestamp DESC
        LIMIT ${len(params) - 1} OFFSET ${len(params)}
        """,
        *params,
    )

    return [
        {
            "id": str(r["id"]),
            "session_id": str(r["session_id"]) if r["session_id"] else None,
            "timestamp": r["timestamp"].isoformat(),
            "tool_name": r["tool_name"],
            "input": r["input"],
            "output": r["output"],
            "status": r["status"],
            "error_message": r["error_message"],
            "duration_ms": r["duration_ms"],
        }
        for r in rows
    ]
