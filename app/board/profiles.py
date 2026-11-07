"""Agent profiles (migration 028): dispatch settings per kind of task, one history row per save."""

import json
from typing import Any

from app.db import get_pool

FIELDS = (
    "model",
    "effort",
    "budget_usd",
    "timeout_s",
    "read_only",
    "evidence_required",
    "description",
)
PROTECTED = "implement"


class ProfileError(Exception):
    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.status = status


def _json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _out(row: dict[str, Any]) -> dict[str, Any]:
    return {k: row[k] for k in (("name",) + FIELDS + ("updated_by", "updated_at", "version"))}


async def list_profiles() -> list[dict[str, Any]]:
    rows = await get_pool().fetch("SELECT * FROM agent_profiles ORDER BY name")
    return [_out(dict(r)) for r in rows]


async def fetch(name: str) -> dict[str, Any] | None:
    row = await get_pool().fetchrow("SELECT * FROM agent_profiles WHERE name = $1", name)
    return _out(dict(row)) if row else None


async def history(name: str) -> list[dict[str, Any]]:
    rows = await get_pool().fetch(
        "SELECT version, author, reason, at, profile FROM agent_profile_versions "
        "WHERE name = $1 ORDER BY version DESC",
        name,
    )
    return [{**dict(r), "profile": _json(r["profile"])} for r in rows]


async def get(name: str) -> dict[str, Any]:
    found = await fetch(name)
    if found is None:
        raise ProfileError(f"no profile {name!r}", 404)
    return {**found, "history": await history(name)}


async def count() -> int:
    return await get_pool().fetchval("SELECT count(*) FROM agent_profiles")


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
                "SELECT COALESCE(MAX(version), 0) FROM agent_profile_versions WHERE name = $1",
                name,
            )
            await conn.execute(
                "INSERT INTO agent_profiles (name, model, effort, budget_usd, timeout_s, "
                "read_only, evidence_required, description, version, updated_by) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)",
                name,
                *(fields[k] for k in FIELDS),
                version,
                author,
            )
        else:
            version = current["version"] + 1
            done = await conn.fetchval(
                "UPDATE agent_profiles SET model = $2, effort = $3, budget_usd = $4, "
                "timeout_s = $5, read_only = $6, evidence_required = $7, description = $8, "
                "version = $9, updated_by = $10, updated_at = NOW() "
                "WHERE name = $1 AND version = $11 RETURNING version",
                name,
                *(fields[k] for k in FIELDS),
                version,
                author,
                current["version"],
            )
            if done is None:
                raise ProfileError("another save landed first; read it again", 409)
        await conn.execute(
            "INSERT INTO agent_profile_versions (name, version, author, reason, profile) "
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
        raise ProfileError(
            f"the profile moved to v{current['version']} since you read v{expected_version}; "
            "read it again and reapply your change",
            409,
        )
    if current and all(current[k] == fields[k] for k in FIELDS):
        raise ProfileError("nothing changed", 422)
    await write(name, {k: fields[k] for k in FIELDS}, current, author, reason)
    return await fetch(name)


async def remove_row(
    name: str, version: int, profile: dict[str, Any], author: str, reason: str
) -> None:
    async with get_pool().acquire() as conn, conn.transaction():
        await conn.execute("DELETE FROM agent_profiles WHERE name = $1", name)
        await conn.execute(
            "INSERT INTO agent_profile_versions (name, version, author, reason, profile) "
            "VALUES ($1, $2, $3, $4, $5::jsonb)",
            name,
            version,
            author,
            reason,
            json.dumps({**profile, "removed": True}),
        )


async def delete(name: str, *, author: str, reason: str) -> dict[str, Any]:
    """Remove a profile, keeping its history; refuses `implement` and the last profile."""
    current = await fetch(name)
    if current is None:
        raise ProfileError(f"no profile {name!r}", 404)
    if name == PROTECTED:
        raise ProfileError(f"{PROTECTED!r} can't be deleted", 409)
    if await count() <= 1:
        raise ProfileError("can't delete the last profile", 409)
    version = current["version"] + 1
    await remove_row(name, version, {k: current[k] for k in FIELDS}, author, reason)
    return {"name": name, "removed": True, "version": version}
