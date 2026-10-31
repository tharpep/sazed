import json
import uuid
from typing import Any

from app.db import get_pool


def _row(rec) -> dict[str, Any]:
    d = dict(rec)
    if isinstance(d.get("routed_to"), str):
        d["routed_to"] = json.loads(d["routed_to"])
    return d


async def create_capture(content: str, source: str = "shortcut") -> dict[str, Any]:
    row = await get_pool().fetchrow(
        """
        INSERT INTO captures (content, source, status)
        VALUES ($1, $2, 'raw')
        RETURNING *
        """,
        content.strip(),
        source.strip(),
    )
    return _row(row)


async def get_capture(capture_id: uuid.UUID) -> dict[str, Any] | None:
    row = await get_pool().fetchrow("SELECT * FROM captures WHERE id = $1", capture_id)
    return _row(row) if row else None


async def list_captures(limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
    rows = await get_pool().fetch(
        "SELECT * FROM captures ORDER BY created_at DESC LIMIT $1 OFFSET $2",
        limit,
        offset,
    )
    return [_row(r) for r in rows]


async def update_capture(
    capture_id: uuid.UUID,
    status: str,
    routed_to: list[dict[str, Any]],
    error: str | None = None,
) -> dict[str, Any] | None:
    row = await get_pool().fetchrow(
        """
        UPDATE captures
        SET status = $2, routed_to = $3::jsonb, error = $4, processed_at = NOW()
        WHERE id = $1
        RETURNING *
        """,
        capture_id,
        status,
        json.dumps(routed_to),
        error,
    )
    return _row(row) if row else None
