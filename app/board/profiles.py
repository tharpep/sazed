"""Agent profiles (migrations 028 and 035): one registry for dispatch tasks and worker jobs, one
history row per save."""

import json
import re
from typing import Any

from app.board import mcp_servers
from app.db import get_pool

FIELDS = (
    "engine",
    "capability",
    "model",
    "effort",
    "budget_usd",
    "timeout_s",
    "max_tool_calls",
    "evidence_required",
    "description",
    "mcp",
)
# What a client from before migration 035 reads; engine is in it because the old worker already
# takes engine from a registry row, and flash needs it.
OLD_KEYS = (
    "name",
    "model",
    "effort",
    "budget_usd",
    "timeout_s",
    "read_only",
    "evidence_required",
    "description",
    "mcp",
    "engine",
    "updated_by",
    "updated_at",
    "version",
)
JOB_PROFILES = frozenset({"research_quick", "research_deep", "second_opinion", "review", "change"})
PROTECTED = frozenset({"implement", "flash", *JOB_PROFILES})
MODELS = {
    "claude": re.compile(r"^(haiku|sonnet|opus|fable|claude-[\w.\-\[\]]+)$"),
    "agy": re.compile(r"^gemini-[\w.\-]+$"),
}


class ProfileError(Exception):
    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.status = status


def _json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _out(row: dict[str, Any]) -> dict[str, Any]:
    keys = ("name",) + FIELDS + ("read_only", "updated_by", "updated_at", "version")
    out = {k: row[k] for k in keys}
    return {**out, "mcp": _json(out["mcp"])}


def shaped(profile: dict[str, Any], v2: bool) -> dict[str, Any]:
    return profile if v2 else {k: profile[k] for k in OLD_KEYS}


def old_client_can_show(profile: dict[str, Any]) -> bool:
    """The old console formats budget as a number and the old worker needs effort and budget."""
    return profile["effort"] is not None and profile["budget_usd"] is not None


def _args(fields: dict[str, Any]) -> list[Any]:
    return [json.dumps(fields[k]) if k == "mcp" else fields[k] for k in FIELDS]


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
                "INSERT INTO agent_profiles (name, engine, capability, model, effort, "
                "budget_usd, timeout_s, max_tool_calls, evidence_required, description, mcp, "
                "version, updated_by) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::jsonb, $12, $13)",
                name,
                *_args(fields),
                version,
                author,
            )
        else:
            version = current["version"] + 1
            done = await conn.fetchval(
                "UPDATE agent_profiles SET engine = $2, capability = $3, model = $4, "
                "effort = $5, budget_usd = $6, timeout_s = $7, max_tool_calls = $8, "
                "evidence_required = $9, description = $10, mcp = $11::jsonb, version = $12, "
                "updated_by = $13, updated_at = NOW() "
                "WHERE name = $1 AND version = $14 RETURNING version",
                name,
                *_args(fields),
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
            json.dumps({**fields, "read_only": fields["capability"] != "change"}),
        )


def _complete(
    fields: dict[str, Any], read_only: bool | None, current: dict[str, Any] | None
) -> dict[str, Any]:
    """Fill what an old client leaves out (engine, capability, max_tool_calls) from the row, so
    its save keeps the row's meaning."""
    engine = fields.get("engine") or (current["engine"] if current else "claude")
    capability = fields.get("capability")
    if capability is None:
        if read_only is None:
            raise ProfileError("send capability (or read_only)", 422)
        if current and (current["capability"] != "change") == read_only:
            capability = current["capability"]
        else:
            capability = "review" if read_only else "change"
    elif read_only is not None and read_only != (capability != "change"):
        raise ProfileError(f"read_only={read_only} contradicts capability {capability!r}", 422)
    if not MODELS[engine].match(fields["model"]):
        raise ProfileError(f"{fields['model']!r} isn't a model for engine {engine!r}", 422)
    max_tool_calls = (
        fields["max_tool_calls"]
        if "max_tool_calls" in fields
        else (current["max_tool_calls"] if current else None)
    )
    return {
        **fields,
        "engine": engine,
        "capability": capability,
        "max_tool_calls": max_tool_calls,
    }


async def save(
    name: str,
    *,
    author: str,
    reason: str,
    expected_version: int | None = None,
    read_only: bool | None = None,
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
    fields = _complete(fields, read_only, current)
    # A job's worker profile (and so its write access) and review_agent_by_source both follow the
    # capability, so a job profile keeps the one it was seeded with.
    if current and name in JOB_PROFILES and fields["capability"] != current["capability"]:
        raise ProfileError(
            f"{name!r} is a job profile; its capability stays {current['capability']!r}", 409
        )
    try:
        await mcp_servers.check_grants(fields["mcp"])
    except mcp_servers.ServerError as e:
        raise ProfileError(str(e), e.status) from e
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
    """Remove a profile, keeping its history; refuses the protected ones and the last profile."""
    current = await fetch(name)
    if current is None:
        raise ProfileError(f"no profile {name!r}", 404)
    if name in PROTECTED:
        raise ProfileError(f"{name!r} can't be deleted", 409)
    if await count() <= 1:
        raise ProfileError("can't delete the last profile", 409)
    version = current["version"] + 1
    snapshot = {**{k: current[k] for k in FIELDS}, "read_only": current["read_only"]}
    await remove_row(name, version, snapshot, author, reason)
    return {"name": name, "removed": True, "version": version}
