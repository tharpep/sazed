"""MCP server registry (migration 029): servers profiles can grant, one history row per save."""

import json
from typing import Any

from app.db import get_pool

FIELDS = ("transport", "url", "command", "args", "headers", "agent_safe", "description")
JSON_FIELDS = ("args", "headers")


class ServerError(Exception):
    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.status = status


def _json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _out(row: dict[str, Any]) -> dict[str, Any]:
    out = {k: row[k] for k in (("name",) + FIELDS + ("version", "updated_by", "updated_at"))}
    return {**out, **{k: _json(out[k]) for k in JSON_FIELDS}}


def _args(fields: dict[str, Any]) -> list[Any]:
    return [json.dumps(fields[k]) if k in JSON_FIELDS else fields[k] for k in FIELDS]


async def list_servers() -> list[dict[str, Any]]:
    rows = await get_pool().fetch("SELECT * FROM mcp_servers ORDER BY name")
    return [_out(dict(r)) for r in rows]


async def fetch(name: str) -> dict[str, Any] | None:
    row = await get_pool().fetchrow("SELECT * FROM mcp_servers WHERE name = $1", name)
    return _out(dict(row)) if row else None


async def history(name: str) -> list[dict[str, Any]]:
    rows = await get_pool().fetch(
        "SELECT version, author, reason, at, server FROM mcp_server_versions "
        "WHERE name = $1 ORDER BY version DESC",
        name,
    )
    return [{**dict(r), "server": _json(r["server"])} for r in rows]


async def get(name: str) -> dict[str, Any]:
    found = await fetch(name)
    if found is None:
        raise ServerError(f"no MCP server {name!r}", 404)
    return {**found, "history": await history(name)}


async def granted_by(name: str) -> list[str]:
    rows = await get_pool().fetch(
        "SELECT name FROM agent_profiles WHERE mcp ? $1 ORDER BY name", name
    )
    return [r["name"] for r in rows]


async def check_grants(mcp: dict[str, list[str]]) -> None:
    for server in mcp:
        found = await fetch(server)
        if found is None:
            raise ServerError(f"no MCP server {server!r}", 422)
        if not found["agent_safe"]:
            raise ServerError(f"MCP server {server!r} is not agent_safe", 422)


async def write(
    name: str,
    fields: dict[str, Any],
    current: dict[str, Any] | None,
    author: str,
    reason: str,
) -> None:
    async with get_pool().acquire() as conn, conn.transaction():
        if current is None:
            version = 1 + await conn.fetchval(
                "SELECT COALESCE(MAX(version), 0) FROM mcp_server_versions WHERE name = $1",
                name,
            )
            await conn.execute(
                "INSERT INTO mcp_servers (name, transport, url, command, args, headers, "
                "agent_safe, description, version, updated_by) "
                "VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb, $7, $8, $9, $10)",
                name,
                *_args(fields),
                version,
                author,
            )
        else:
            version = current["version"] + 1
            done = await conn.fetchval(
                "UPDATE mcp_servers SET transport = $2, url = $3, command = $4, "
                "args = $5::jsonb, headers = $6::jsonb, agent_safe = $7, description = $8, "
                "version = $9, updated_by = $10, updated_at = NOW() "
                "WHERE name = $1 AND version = $11 RETURNING version",
                name,
                *_args(fields),
                version,
                author,
                current["version"],
            )
            if done is None:
                raise ServerError("another save landed first; read it again", 409)
        await conn.execute(
            "INSERT INTO mcp_server_versions (name, version, author, reason, server) "
            "VALUES ($1, $2, $3, $4, $5::jsonb)",
            name,
            version,
            author,
            reason,
            json.dumps(fields),
        )


async def save(
    name: str,
    *,
    author: str,
    reason: str,
    expected_version: int | None = None,
    **fields: Any,
) -> dict[str, Any] | None:
    """Create or update; refuses a stale base and a save that changes nothing."""
    current = await fetch(name)
    if current and expected_version is not None and expected_version != current["version"]:
        raise ServerError(
            f"the server moved to v{current['version']} since you read v{expected_version}; "
            "read it again and reapply your change",
            409,
        )
    if current and all(current[k] == fields[k] for k in FIELDS):
        raise ServerError("nothing changed", 422)
    await write(name, {k: fields[k] for k in FIELDS}, current, author, reason)
    return await fetch(name)


async def remove_row(
    name: str, version: int, server: dict[str, Any], author: str, reason: str
) -> None:
    async with get_pool().acquire() as conn, conn.transaction():
        await conn.execute("DELETE FROM mcp_servers WHERE name = $1", name)
        await conn.execute(
            "INSERT INTO mcp_server_versions (name, version, author, reason, server) "
            "VALUES ($1, $2, $3, $4, $5::jsonb)",
            name,
            version,
            author,
            reason,
            json.dumps({**server, "removed": True}),
        )


async def delete(name: str, *, author: str, reason: str) -> dict[str, Any]:
    """Remove a server, keeping its history; refuses while a profile grants it."""
    current = await fetch(name)
    if current is None:
        raise ServerError(f"no MCP server {name!r}", 404)
    granting = await granted_by(name)
    if granting:
        raise ServerError(f"profiles still grant {name!r}: {', '.join(granting)}", 409)
    version = current["version"] + 1
    await remove_row(name, version, {k: current[k] for k in FIELDS}, author, reason)
    return {"name": name, "removed": True, "version": version}
