"""Postgres access for backlog_items (migration 005), plus dedupe helpers."""

import json
import re
import uuid
from difflib import SequenceMatcher
from typing import Any

from app.db import get_pool

TYPES = ("idea", "bug", "gap", "optimization", "planned", "known_issue")
OPEN = ("new", "shortlisted")
STATUSES = ("new", "shortlisted", "approved", "done", "dismissed", "stale")

_STOP = set(
    "a an the and or for to of in on with into via by is are be use using add support new "
    "better improve your my sazed homelab".split()
)


def fingerprint(title: str) -> str:
    """Sorted significant words: 'Add Proxmox Backup Server' -> 'backup proxmox server'."""
    words = re.findall(r"[a-z0-9]+", title.lower())
    return " ".join(sorted({w for w in words if w not in _STOP and len(w) > 1}))


def similar(a: str, b: str) -> bool:
    """Two titles describe the same thing (same fingerprint, or close wording/word overlap)."""
    fa, fb = fingerprint(a), fingerprint(b)
    if not fa or not fb:
        return False
    if fa == fb:
        return True
    wa, wb = set(fa.split()), set(fb.split())
    overlap = len(wa & wb) / max(1, min(len(wa), len(wb)))
    return overlap >= 0.75 or SequenceMatcher(None, a.lower(), b.lower()).ratio() >= 0.82


def _row(rec) -> dict[str, Any]:
    d = dict(rec)
    if isinstance(d.get("sources"), str):
        d["sources"] = json.loads(d["sources"])
    if isinstance(d.get("job_meta"), str):
        d["job_meta"] = json.loads(d["job_meta"])
    if isinstance(d.get("job_params"), str):
        d["job_params"] = json.loads(d["job_params"])
    return d


_SELECT = """
    SELECT b.*, j.status AS job_status, j.meta AS job_meta, j.params AS job_params
    FROM backlog_items b LEFT JOIN jobs j ON j.id = b.job_id
"""


async def list_items(
    type_: str | None = None, view: str = "open", limit: int = 200
) -> list[dict[str, Any]]:
    """view: open (new, shortlisted), done (approved, done), dismissed (dismissed, stale), all."""
    statuses = {
        "open": list(OPEN),
        "done": ["approved", "done"],
        "dismissed": ["dismissed", "stale"],
        "all": list(STATUSES),
    }[view]
    rows = await get_pool().fetch(
        _SELECT
        + """
        WHERE ($1::text IS NULL OR b.type = $1) AND b.status = ANY($2::text[])
        ORDER BY b.rank NULLS LAST, b.created_at DESC
        LIMIT $3
        """,
        type_,
        statuses,
        limit,
    )
    return [_row(r) for r in rows]


async def get_item(item_id: uuid.UUID) -> dict[str, Any] | None:
    rec = await get_pool().fetchrow(_SELECT + " WHERE b.id = $1", item_id)
    return _row(rec) if rec else None


async def all_titles() -> list[str]:
    """Every title ever recorded, dismissed and stale included (the scout must not repeat them)."""
    return [r["title"] for r in await get_pool().fetch("SELECT title FROM backlog_items")]


async def find_duplicate(title: str) -> dict[str, Any] | None:
    for rec in await get_pool().fetch("SELECT id, title, status FROM backlog_items"):
        if similar(title, rec["title"]):
            return dict(rec)
    return None


async def create_item(
    *,
    type_: str,
    title: str,
    source: str,
    summary: str = "",
    why: str = "",
    repos: list[str] | None = None,
    effort: str | None = None,
    impact: str | None = None,
    sources: list[str] | None = None,
) -> dict[str, Any]:
    rec = await get_pool().fetchrow(
        """
        INSERT INTO backlog_items
            (type, title, summary, why, repos, effort, impact, sources, fingerprint, source)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9, $10)
        RETURNING *
        """,
        type_,
        title.strip(),
        summary.strip(),
        why.strip(),
        repos or [],
        effort,
        impact,
        json.dumps(sources or []),
        fingerprint(title),
        source,
    )
    return _row(rec)


async def update_item(item_id: uuid.UUID, **fields: Any) -> dict[str, Any] | None:
    allowed = {"status", "note", "rank", "job_id", "type", "title", "summary", "why", "repos",
               "effort", "impact"}
    sets, args = [], []
    for key, value in fields.items():
        if key not in allowed:
            raise ValueError(f"can't update {key}")
        args.append(value)
        sets.append(f"{key} = ${len(args)}")
    if "status" in fields and fields["status"] in ("approved", "dismissed", "done"):
        sets.append("decided_at = NOW()")
    if "title" in fields:
        args.append(fingerprint(fields["title"]))
        sets.append(f"fingerprint = ${len(args)}")
    args.append(item_id)
    rec = await get_pool().fetchrow(
        f"UPDATE backlog_items SET {', '.join(sets)}, updated_at = NOW() "
        f"WHERE id = ${len(args)} RETURNING id",
        *args,
    )
    if rec and "status" in fields:
        await normalize_ranks()  # leaving (or re-entering) the open views shifts the order
    return await get_item(item_id) if rec else None


async def mark_stale(days: int = 30) -> int:
    """Open ideas nobody acted on go stale (kept for dedupe, out of the open list)."""
    result = await get_pool().execute(
        f"""
        UPDATE backlog_items SET status = 'stale', updated_at = NOW()
        WHERE type = 'idea' AND status = 'new' AND created_at < NOW() - INTERVAL '{int(days)} days'
        """
    )
    return int(result.split()[-1])


# Priority (rank) is Pryce's saved pick-up order across open items: 1 = next. It changes only when
# he moves something, and stays contiguous: items that leave the open views give up their rank.
async def _renumber(conn, ordered_ids: list[uuid.UUID]) -> None:
    await conn.execute(
        """
        UPDATE backlog_items SET rank = NULL
        WHERE rank IS NOT NULL AND (status <> ALL($1::text[]) OR id <> ALL($2::uuid[]))
        """,
        list(OPEN),
        ordered_ids,
    )
    for n, item_id in enumerate(ordered_ids, start=1):
        await conn.execute(
            "UPDATE backlog_items SET rank = $2, updated_at = NOW() WHERE id = $1 AND rank IS "
            "DISTINCT FROM $2",
            item_id,
            n,
        )


async def _ranked_open(conn) -> list[uuid.UUID]:
    rows = await conn.fetch(
        """
        SELECT id FROM backlog_items
        WHERE rank IS NOT NULL AND status = ANY($1::text[])
        ORDER BY rank, created_at
        """,
        list(OPEN),
    )
    return [r["id"] for r in rows]


async def normalize_ranks() -> None:
    """Close gaps after items are approved, done or dismissed."""
    async with get_pool().acquire() as conn, conn.transaction():
        await _renumber(conn, await _ranked_open(conn))


async def move_item(item_id: uuid.UUID, position: int | None) -> dict[str, Any] | None:
    """Put an open item at `position` (1 = top; past the end = last), or unrank it with None."""
    async with get_pool().acquire() as conn, conn.transaction():
        status = await conn.fetchval("SELECT status FROM backlog_items WHERE id = $1", item_id)
        if status is None:
            return None
        if status not in OPEN:
            raise ValueError(f"item is {status}; only open items have a priority")
        order = [i for i in await _ranked_open(conn) if i != item_id]
        if position is not None:
            order.insert(max(0, min(position - 1, len(order))), item_id)
        await _renumber(conn, order)
    return await get_item(item_id)
