"""Plans on the board (migration 023): versions with an author and a reason, sections to cards."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from app.board import plan_text, service
from app.db import get_pool

STATES = ("draft", "active", "done", "superseded")
STALE_AFTER = timedelta(days=14)


class PlanError(ValueError):
    pass


_SELECT = """
    SELECT p.*, pr.slug AS project_slug, pr.title AS project_title,
           v.author AS last_author, v.reason AS last_reason, v.created_at AS last_saved_at
    FROM plans p
    LEFT JOIN projects pr ON pr.id = p.project_id
    LEFT JOIN plan_versions v ON v.plan_id = p.id AND v.version = p.current_version
"""


def _summary(row: dict[str, Any], cards: list[dict[str, Any]]) -> dict[str, Any]:
    mine = [c for c in cards if c.get("plan_id") == str(row["id"])]
    saved = row["last_saved_at"]
    moved = [c for c in mine if saved and c["updated_at"] > saved]
    stale = bool(
        row["state"] == "active" and saved and moved and datetime.now(UTC) - saved > STALE_AFTER
    )
    return {
        "id": str(row["id"]),
        "slug": row["slug"],
        "title": row["title"],
        "state": row["state"],
        "project": row["project_slug"],
        "project_title": row["project_title"],
        "workstream_id": str(row["workstream_id"]) if row["workstream_id"] else None,
        "version": row["current_version"],
        "last_author": row["last_author"],
        "last_reason": row["last_reason"],
        "last_saved_at": saved,
        "cards_total": len(mine),
        "cards_done": sum(c["stage"] == "done" for c in mine),
        "cards_moved_since": len(moved),
        "stale": stale,
    }


async def _cards() -> list[dict[str, Any]]:
    return await service.cards(all_done=True)


async def list_plans(project: str | None = None, state: str | None = None) -> list[dict[str, Any]]:
    rows = await get_pool().fetch(
        _SELECT
        + """
        WHERE ($1::text IS NULL OR pr.slug = $1) AND ($2::text IS NULL OR p.state = $2)
        ORDER BY (p.state = 'active') DESC, v.created_at DESC NULLS LAST
        """,
        project,
        state,
    )
    cards = await _cards()
    return [_summary(dict(r), cards) for r in rows]


async def _row(slug: str) -> dict[str, Any]:
    rec = await get_pool().fetchrow(_SELECT + " WHERE p.slug = $1", slug)
    if rec is None:
        raise PlanError(f"no plan {slug!r}")
    return dict(rec)


async def _body(plan_id: uuid.UUID, version: int) -> str:
    body = await get_pool().fetchval(
        "SELECT body FROM plan_versions WHERE plan_id = $1 AND version = $2", plan_id, version
    )
    if body is None:
        raise PlanError(f"no version {version}")
    return body


async def get_plan(slug: str, version: int | None = None) -> dict[str, Any]:
    """One plan at a version (default the latest): body, sections with their cards, summary."""
    row = await _row(slug)
    v = version or row["current_version"]
    body = await _body(row["id"], v)
    cards = await _cards()
    out = _summary(row, cards) | {"shown_version": v, "body": body}
    mine = [c for c in cards if c.get("plan_id") == str(row["id"])]
    old_bodies: dict[int, str] = {}
    secs = []
    for s in plan_text.sections(body):
        linked = []
        for c in [c for c in mine if c.get("plan_section") == s["id"]]:
            changed = False
            filed = c.get("plan_version")
            if filed and filed < row["current_version"]:
                if filed not in old_bodies:
                    old_bodies[filed] = await _body(row["id"], filed)
                then = plan_text.section_text(old_bodies[filed], s["id"])
                changed = then is not None and plan_text._norm(then) != plan_text._norm(s["text"])
            linked.append(c | {"section_changed_since_filed": changed})
        secs.append(
            {
                "id": s["id"],
                "title": s["title"],
                "level": s["level"],
                "text": s["text"],
                "cards": linked,
            }
        )
    out["preamble"] = plan_text.preamble(body)
    out["sections"] = secs
    out["unlinked_cards"] = [
        c
        for c in mine
        if not c.get("plan_section") or c["plan_section"] not in {s["id"] for s in secs}
    ]
    return out


async def versions(slug: str) -> list[dict[str, Any]]:
    row = await _row(slug)
    rows = await get_pool().fetch(
        "SELECT version, author, reason, created_at, length(body) AS chars FROM plan_versions "
        "WHERE plan_id = $1 ORDER BY version DESC",
        row["id"],
    )
    return [dict(r) for r in rows]


async def diff(slug: str, a: int, b: int) -> dict[str, Any]:
    row = await _row(slug)
    meta = {r["version"]: r for r in await versions(slug)}
    if a not in meta or b not in meta:
        raise PlanError("both versions must exist")
    old, new = await _body(row["id"], a), await _body(row["id"], b)
    return {"from": meta[a], "to": meta[b], "blocks": plan_text.diff(old, new)}


async def create(
    *,
    slug: str,
    title: str,
    body: str,
    author: str,
    reason: str,
    project: str | None = None,
    workstream_id: uuid.UUID | None = None,
    state: str = "draft",
) -> dict[str, Any]:
    pool = get_pool()
    if await pool.fetchval("SELECT 1 FROM plans WHERE slug = $1", slug):
        raise PlanError(f"plan {slug!r} already exists")
    project_id = None
    if project:
        project_id = await pool.fetchval("SELECT id FROM projects WHERE slug = $1", project)
        if project_id is None:
            raise PlanError(f"no project {project!r}")
    async with pool.acquire() as conn, conn.transaction():
        pid = await conn.fetchval(
            "INSERT INTO plans (slug, title, project_id, workstream_id, state, current_version) "
            "VALUES ($1, $2, $3, $4, $5, 1) RETURNING id",
            slug,
            title,
            project_id,
            workstream_id,
            state,
        )
        await conn.execute(
            "INSERT INTO plan_versions (plan_id, version, body, author, reason) "
            "VALUES ($1, 1, $2, $3, $4)",
            pid,
            body,
            author,
            reason,
        )
    return await get_plan(slug)


async def save(
    slug: str, *, body: str, author: str, reason: str, expected_version: int | None = None
) -> dict[str, Any]:
    """A new version. Refuses a stale base, a no-op, and a rewrite that doesn't say it is one."""
    row = await _row(slug)
    current = row["current_version"]
    if expected_version is not None and expected_version != current:
        raise PlanError(
            f"the plan moved to v{current} since you read v{expected_version}; "
            "read it again and reapply your change"
        )
    old = await _body(row["id"], current)
    if plan_text._norm(old) == plan_text._norm(body):
        raise PlanError("nothing changed")
    if plan_text.is_rewrite(old, body) and not reason.lower().startswith("rewrite:"):
        raise PlanError(
            "this replaces more than half the plan; start the reason with "
            "'rewrite:' if that's intended"
        )
    async with get_pool().acquire() as conn, conn.transaction():
        new = await conn.fetchval(
            "UPDATE plans SET current_version = current_version + 1, updated_at = NOW() "
            "WHERE id = $1 AND current_version = $2 RETURNING current_version",
            row["id"],
            current,
        )
        if new is None:
            raise PlanError("another save landed first; read it again")
        await conn.execute(
            "INSERT INTO plan_versions (plan_id, version, body, author, reason) "
            "VALUES ($1, $2, $3, $4, $5)",
            row["id"],
            new,
            body,
            author,
            reason,
        )
    return await get_plan(slug)


async def update(slug: str, **fields: Any) -> dict[str, Any]:
    row = await _row(slug)
    sets, args = [], []
    if "project" in fields:
        project = fields.pop("project")
        pid = await get_pool().fetchval("SELECT id FROM projects WHERE slug = $1", project)
        if project and pid is None:
            raise PlanError(f"no project {project!r}")
        fields["project_id"] = pid
    for key, value in fields.items():
        if key not in ("title", "state", "project_id", "workstream_id"):
            raise PlanError(f"can't update {key}")
        args.append(value)
        sets.append(f"{key} = ${len(args)}")
    if sets:
        args.append(row["id"])
        await get_pool().execute(
            f"UPDATE plans SET {', '.join(sets)}, updated_at = NOW() WHERE id = ${len(args)}",
            *args,
        )
    return await get_plan(slug)


async def plan_id(slug: str) -> uuid.UUID:
    return (await _row(slug))["id"]
