"""Plan states on a card (migration 041, slice 4): approval, start, and the card's history.

The board's item query stays as it is; these columns are read here and merged into each item, so
a card can say who approved its plan, who started it, and whether its plan section changed since.
"""

import json
import uuid
from typing import Any

import asyncpg

from app.board import plan_text
from app.board.store import OFF_BOARD
from app.db import get_pool

OWNER = "pryce"
# ASCII only after "agent:", so a lookalike name (Cyrillic, accented, fullwidth) cannot pass.
ACTOR = r"^(pryce|agent:[a-z0-9][a-z0-9._-]{0,63})$"

KINDS = (
    "plan_approved",
    "started",
    "approval_withdrawn",
    "start_withdrawn",
    "pr_linked",
    "pr_link_ambiguous",
)

# Sazed's own writes from a GitHub event. ACTOR cannot match it, so no API caller can claim it
# (pinned by test_the_server_actor_is_barred_from_the_api).
SERVER_ACTOR = "sazed:github"

# Start refuses a card whose plan is archived; a draft plan may still be built.
CLOSED_PLAN_STATES = ("done", "superseded")

Db = asyncpg.Connection | asyncpg.Pool

_STATES = """
    SELECT b.id, b.status, b.rank, b.workstream_id, b.plan_id, b.plan_section,
           b.plan_approved_at, b.plan_approved_by, b.plan_approved_plan_id,
           b.plan_approved_section, b.plan_approved_version, b.started_at, b.started_by,
           p.slug AS plan_slug, p.state AS plan_state, p.current_version AS plan_current_version,
           ap.slug AS approved_plan_slug
    FROM backlog_items b
    LEFT JOIN plans p ON p.id = b.plan_id
    LEFT JOIN plans ap ON ap.id = b.plan_approved_plan_id
    WHERE ($1::uuid IS NULL AND b.status <> ALL($2::text[])) OR b.id = $1
"""

# /board/command polls every few seconds, so each plan version's body is fetched once per read,
# however many cards approved it (pinned by test_each_plan_body_is_fetched_once_per_read).
_BODIES = """
    SELECT v.plan_id, v.version, v.body
    FROM plan_versions v
    JOIN unnest($1::uuid[], $2::int[]) AS w (plan_id, version)
      ON v.plan_id = w.plan_id AND v.version = w.version
"""

Body = tuple[uuid.UUID, int]


def _edit_check(row: dict[str, Any]) -> tuple[Body, Body] | None:
    """The approved and current bodies to compare, for an approval still on the plan and section
    it approved whose plan was saved since; None when no text can have changed it."""
    if not (row["plan_approved_at"] and row["plan_section"] and row["plan_current_version"]):
        return None
    if (row["plan_approved_plan_id"], row["plan_approved_section"]) != (
        row["plan_id"],
        row["plan_section"],
    ):
        return None
    if row["plan_approved_version"] is None:
        return None
    if row["plan_approved_version"] >= row["plan_current_version"]:
        return None
    return (row["plan_id"], row["plan_approved_version"]), (
        row["plan_id"],
        row["plan_current_version"],
    )


async def _with_bodies(db: Db, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    checks = [_edit_check(r) for r in rows]
    wanted = sorted({key for pair in checks if pair for key in pair})
    bodies: dict[Body, str] = {}
    if wanted:
        fetched = await db.fetch(_BODIES, [k[0] for k in wanted], [k[1] for k in wanted])
        bodies = {(r["plan_id"], r["version"]): r["body"] for r in fetched}
    return [
        r | ({"approved_body": bodies.get(c[0]), "current_body": bodies.get(c[1])} if c else {})
        for r, c in zip(rows, checks, strict=True)
    ]


def changed_since(approved_body: str | None, current_body: str | None, section: str | None) -> bool:
    """The rule plans.get_plan uses for section_changed_since_filed: the section's text at the
    approved version differs from its text now. A section missing then is not a change."""
    if not (approved_body and current_body and section):
        return False
    then = plan_text.section_text(approved_body, section)
    now = plan_text.section_text(current_body, section)
    return then is not None and now is not None and plan_text._norm(then) != plan_text._norm(now)


def stale(row: dict[str, Any]) -> bool:
    """An approval no longer covers the card: it now links another plan or section than the one
    approved (null included, so linking, unlinking and moving all count), or the approved
    section's text changed since."""
    if not row.get("plan_approved_at"):
        return False
    if row.get("plan_approved_plan_id") != row.get("plan_id"):
        return True
    if row.get("plan_approved_section") != row.get("plan_section"):
        return True
    return changed_since(row.get("approved_body"), row.get("current_body"), row.get("plan_section"))


def _state(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "plan_approved_at": row["plan_approved_at"],
        "plan_approved_by": row["plan_approved_by"],
        "plan_approved_plan_id": row["plan_approved_plan_id"],
        "plan_approved_section": row["plan_approved_section"],
        "plan_approved_version": row["plan_approved_version"],
        "approved_plan_slug": row["approved_plan_slug"],
        "started_at": row["started_at"],
        "started_by": row["started_by"],
        "status": row["status"],
        "rank": row["rank"],
        "workstream_id": row["workstream_id"],
        "plan_id": row["plan_id"],
        "plan_section": row["plan_section"],
        "plan_slug": row["plan_slug"],
        "plan_state": row["plan_state"],
        "plan_active": row["plan_state"] == "active",
        "plan_current_version": row["plan_current_version"],
        "plan_changed": stale(row),
    }


async def states(db: Db | None = None) -> dict[uuid.UUID, dict[str, Any]]:
    """Every card's plan state, keyed by item id."""
    db = db or get_pool()
    rows = [dict(r) for r in await db.fetch(_STATES, None, list(OFF_BOARD))]
    return {r["id"]: _state(r) for r in await _with_bodies(db, rows)}


async def state(item_id: uuid.UUID, db: Db | None = None) -> dict[str, Any] | None:
    db = db or get_pool()
    row = await db.fetchrow(_STATES, item_id, list(OFF_BOARD))
    if row is None:
        return None
    [found] = await _with_bodies(db, [dict(row)])
    return _state(found)


def approval_block(item: dict[str, Any]) -> dict[str, Any] | None:
    if not item.get("plan_approved_at"):
        return None
    return {
        "at": item["plan_approved_at"],
        "by": item["plan_approved_by"],
        "plan": item.get("approved_plan_slug"),
        "version": item["plan_approved_version"],
        "section": item.get("plan_approved_section"),
        "changed_since": bool(item.get("plan_changed")),
    }


def started_block(item: dict[str, Any]) -> dict[str, Any] | None:
    if not item.get("started_at"):
        return None
    return {"at": item["started_at"], "by": item["started_by"]}


async def record(
    db: Db, item_id: uuid.UUID, kind: str, actor: str, detail: dict[str, Any] | None = None
) -> None:
    await db.execute(
        "INSERT INTO card_events (item_id, kind, actor, detail) VALUES ($1, $2, $3, $4::jsonb)",
        item_id,
        kind,
        actor,
        json.dumps(detail or {}),
    )


async def record_ambiguous(
    db: Db, item_id: uuid.UUID, repo: str, number: int, candidates: list[uuid.UUID]
) -> bool:
    """One pr_link_ambiguous row per card and PR, however often the event comes back. The caller
    holds a transaction; the advisory lock keeps two racing events from both inserting."""
    await db.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
        f"pr_link_ambiguous:{item_id}:{repo.lower()}#{number}",
    )
    row = await db.fetchrow(
        """
        INSERT INTO card_events (item_id, kind, actor, detail)
        SELECT $1, 'pr_link_ambiguous', $2, $3::jsonb
        WHERE NOT EXISTS (
            SELECT 1 FROM card_events
            WHERE item_id = $1 AND kind = 'pr_link_ambiguous'
              AND lower(detail->>'repo') = lower($4) AND detail->>'number' = $5
        )
        RETURNING id
        """,
        item_id,
        SERVER_ACTOR,
        json.dumps(
            {"repo": repo, "number": number, "candidates": [f"item:{c}" for c in candidates]}
        ),
        repo,
        str(number),
    )
    return row is not None


async def history(item_id: uuid.UUID) -> list[dict[str, Any]]:
    """The card's approvals, starts, withdrawals and PR links, oldest first."""
    rows = await get_pool().fetch(
        "SELECT at, kind, actor, detail FROM card_events WHERE item_id = $1 ORDER BY at, id",
        item_id,
    )
    out = []
    for r in rows:
        detail = r["detail"]
        out.append(
            {
                "at": r["at"],
                "kind": r["kind"],
                "actor": r["actor"],
                "detail": json.loads(detail) if isinstance(detail, str) else (detail or {}),
            }
        )
    return out
