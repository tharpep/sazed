"""The approvals table: things waiting on Pryce (migration 009)."""

import json
import uuid
from typing import Any

from app.db import get_pool


def _row(rec) -> dict[str, Any]:
    row = dict(rec)
    if isinstance(row.get("payload"), str):
        row["payload"] = json.loads(row["payload"])
    return row


async def create(kind: str, title: str, requested_by: str, payload: dict[str, Any]) -> uuid.UUID:
    return await get_pool().fetchval(
        "INSERT INTO approvals (kind, title, requested_by, payload) "
        "VALUES ($1, $2, $3, $4::jsonb) RETURNING id",
        kind,
        title[:500],
        requested_by,
        json.dumps(payload),
    )


async def list_approvals(status: str = "pending", limit: int = 50) -> list[dict[str, Any]]:
    await expire_old()
    rows = await get_pool().fetch(
        "SELECT id, kind, title, requested_by, status, created_at, expires_at, decided_at "
        "FROM approvals WHERE status = $1 ORDER BY created_at DESC LIMIT $2",
        status,
        limit,
    )
    return [dict(r) for r in rows]


async def decide(approval_id: uuid.UUID, verdict: str) -> dict[str, Any] | None:
    """Approve or deny a pending, unexpired approval. Returns the row, or None if it can't be."""
    await expire_old()
    rec = await get_pool().fetchrow(
        "UPDATE approvals SET status = $2, decided_at = NOW() "
        "WHERE id = $1 AND status = 'pending' RETURNING *",
        approval_id,
        verdict,
    )
    return _row(rec) if rec else None


async def expire_old() -> None:
    await get_pool().execute(
        "UPDATE approvals SET status = 'expired', decided_at = NOW() "
        "WHERE status = 'pending' AND expires_at < NOW()"
    )
