"""Projects and workstreams (migration 021): reads with computed rollups, and the writes agents
and the console use to keep them current."""

import uuid
from typing import Any

from app.board import rollup, service
from app.db import get_pool


class ProjectError(ValueError):
    pass


_PROJECT_FIELDS = {"title", "summary", "state", "rank", "repos", "plan", "latest_update"}
_WORKSTREAM_FIELDS = {"title", "summary", "position", "state", "depends_on"}


async def _rows(include_closed: bool) -> list[dict[str, Any]]:
    rows = await get_pool().fetch(
        """
        SELECT * FROM projects
        WHERE $1 OR state IN ('active', 'paused')
        ORDER BY rank NULLS LAST, created_at
        """,
        include_closed,
    )
    return [dict(r) for r in rows]


async def _workstreams(project_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[dict[str, Any]]]:
    rows = await get_pool().fetch(
        "SELECT * FROM workstreams WHERE project_id = ANY($1::uuid[]) ORDER BY position",
        project_ids,
    )
    result: dict[uuid.UUID, list[dict[str, Any]]] = {}
    for r in rows:
        result.setdefault(r["project_id"], []).append(dict(r))
    return result


def _by_workstream(cards: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for card in cards:
        if card.get("workstream_id"):
            result.setdefault(card["workstream_id"], []).append(card)
    return result


async def _build(rows: list[dict[str, Any]], with_cards: bool) -> list[dict[str, Any]]:
    streams = await _workstreams([r["id"] for r in rows])
    by_ws = _by_workstream(await service.cards(all_done=True))
    result = []
    for row in rows:
        steps = rollup.workstreams(streams.get(row["id"], []), by_ws)
        if not with_cards:
            for step in steps:
                step.pop("cards")
        result.append(rollup.project(row, steps))
    return result


async def list_projects(include_closed: bool = False) -> list[dict[str, Any]]:
    return await _build(await _rows(include_closed), with_cards=False)


async def get_project(slug: str) -> dict[str, Any] | None:
    row = await get_pool().fetchrow("SELECT * FROM projects WHERE slug = $1", slug)
    if row is None:
        return None
    return (await _build([dict(row)], with_cards=True))[0]


async def _project_id(slug: str) -> uuid.UUID:
    pid = await get_pool().fetchval("SELECT id FROM projects WHERE slug = $1", slug)
    if pid is None:
        raise ProjectError(f"no project {slug!r}")
    return pid


async def create_project(*, slug: str, title: str, **fields: Any) -> dict[str, Any]:
    if await get_pool().fetchval("SELECT 1 FROM projects WHERE slug = $1", slug):
        raise ProjectError(f"project {slug!r} already exists")
    await get_pool().execute("INSERT INTO projects (slug, title) VALUES ($1, $2)", slug, title)
    if fields:
        await update_project(slug, **fields)
    return await get_project(slug)


async def update_project(slug: str, **fields: Any) -> dict[str, Any]:
    pid = await _project_id(slug)
    sets, args = [], []
    for key, value in fields.items():
        if key not in _PROJECT_FIELDS:
            raise ProjectError(f"can't update {key}")
        args.append(value)
        sets.append(f"{key} = ${len(args)}")
    if "latest_update" in fields:
        sets.append("latest_update_at = NOW()")
    if sets:
        args.append(pid)
        await get_pool().execute(
            f"UPDATE projects SET {', '.join(sets)}, updated_at = NOW() WHERE id = ${len(args)}",
            *args,
        )
    return await get_project(slug)


async def _check_depends(project_id: uuid.UUID, ws_id: uuid.UUID | None, deps: list[uuid.UUID]):
    if ws_id is not None and ws_id in deps:
        raise ProjectError("a workstream can't depend on itself")
    found = await get_pool().fetchval(
        "SELECT count(*) FROM workstreams WHERE project_id = $1 AND id = ANY($2::uuid[])",
        project_id,
        deps,
    )
    if found != len(set(deps)):
        raise ProjectError("depends_on must name workstreams in the same project")


async def create_workstream(slug: str, *, title: str, **fields: Any) -> dict[str, Any]:
    pid = await _project_id(slug)
    deps = fields.pop("depends_on", None) or []
    await _check_depends(pid, None, deps)
    position = fields.pop("position", None)
    if position is None:
        position = await get_pool().fetchval(
            "SELECT COALESCE(MAX(position), 0) + 1 FROM workstreams WHERE project_id = $1", pid
        )
    ws_id = await get_pool().fetchval(
        """
        INSERT INTO workstreams (project_id, title, position, depends_on)
        VALUES ($1, $2, $3, $4) RETURNING id
        """,
        pid,
        title,
        position,
        deps,
    )
    if fields:
        await update_workstream(ws_id, **fields)
    return await workstream(ws_id)


async def update_workstream(ws_id: uuid.UUID, **fields: Any) -> dict[str, Any]:
    pid = await get_pool().fetchval("SELECT project_id FROM workstreams WHERE id = $1", ws_id)
    if pid is None:
        raise ProjectError(f"no workstream {ws_id}")
    if "depends_on" in fields:
        await _check_depends(pid, ws_id, fields["depends_on"])
    sets, args = [], []
    for key, value in fields.items():
        if key not in _WORKSTREAM_FIELDS:
            raise ProjectError(f"can't update {key}")
        args.append(value)
        sets.append(f"{key} = ${len(args)}")
    if sets:
        args.append(ws_id)
        await get_pool().execute(
            f"UPDATE workstreams SET {', '.join(sets)}, updated_at = NOW() WHERE id = ${len(args)}",
            *args,
        )
    return await workstream(ws_id)


async def workstream(ws_id: uuid.UUID) -> dict[str, Any]:
    """The workstream as its project's ladder shows it, cards included."""
    slug = await get_pool().fetchval(
        "SELECT p.slug FROM workstreams w JOIN projects p ON p.id = w.project_id WHERE w.id = $1",
        ws_id,
    )
    if slug is None:
        raise ProjectError(f"no workstream {ws_id}")
    found = await get_project(slug)
    step = next(w for w in found["workstreams"] if w["id"] == str(ws_id))
    return step | {"project": slug}


async def workstream_exists(ws_id: uuid.UUID) -> bool:
    return bool(await get_pool().fetchval("SELECT 1 FROM workstreams WHERE id = $1", ws_id))
