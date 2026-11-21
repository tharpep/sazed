"""SQL for Claude Code sessions and their steps (migration 039)."""

from collections.abc import Callable
from datetime import datetime
from typing import Any

from app.db import get_pool

STEPS_PER_AGENT = 80

_SESSION_COLUMNS = (
    "session_id",
    "host",
    "cwd",
    "repo",
    "branch",
    "card_id",
    "started_at",
    "meta_ts",
    "meta_seq",
    "state",
    "state_ts",
    "state_seq",
    "ended_at",
    "doing",
    "doing_source",
    "doing_ts",
    "doing_seq",
    "question",
    "question_agent",
    "question_ts",
    "question_seq",
    "cleared_ts",
    "cleared_seq",
)

_META = ("host", "cwd", "repo", "branch", "card_id")
_QUESTION_COLUMNS = ("question", "question_agent", "question_ts", "question_seq")


def _newer(pair: str) -> str:
    """The batch's (ts, seq) for `pair` is newer than the stored one; a NULL stored pair loses."""
    return (
        f"(EXCLUDED.{pair}_ts, EXCLUDED.{pair}_seq)"
        f" > (COALESCE(s.{pair}_ts, -1), COALESCE(s.{pair}_seq, -1))"
    )


def _take(columns: tuple[str, ...], pair: str) -> list[str]:
    return [f"{c} = CASE WHEN {_newer(pair)} THEN EXCLUDED.{c} ELSE s.{c} END" for c in columns]


# Identity columns: a newer batch replaces them, an older one only fills what is still missing.
_META_SET = [
    f"{c} = CASE WHEN (EXCLUDED.meta_ts, EXCLUDED.meta_seq) >= (s.meta_ts, s.meta_seq)"
    f" THEN COALESCE(EXCLUDED.{c}, s.{c}) ELSE COALESCE(s.{c}, EXCLUDED.{c}) END"
    for c in _META
]

# Every SET expression reads the old row, so the pair columns can be assigned in any order.
_UPSERT = f"""
    INSERT INTO agent_sessions AS s ({", ".join(_SESSION_COLUMNS)})
    VALUES ({", ".join(f"${n}" for n in range(1, len(_SESSION_COLUMNS) + 1))})
    ON CONFLICT (session_id) DO UPDATE SET
        {", ".join(_META_SET)},
        {", ".join(_take(("meta_ts", "meta_seq"), "meta"))},
        started_at = LEAST(s.started_at, EXCLUDED.started_at),
        last_heard_at = NOW(),
        {", ".join(_take(("state", "ended_at", "state_ts", "state_seq"), "state"))},
        {", ".join(_take(("doing", "doing_source", "doing_ts", "doing_seq"), "doing"))},
        {", ".join(_take(_QUESTION_COLUMNS, "question"))},
        {", ".join(_take(("cleared_ts", "cleared_seq"), "cleared"))}
"""

_OPEN_QUESTIONS = """
    SELECT session_id, question_agent, question_ts, question_seq FROM agent_sessions
    WHERE session_id = ANY($1::text[])
    ORDER BY session_id
    FOR UPDATE
"""

_INSERT_EVENTS = """
    INSERT INTO agent_session_events
        (session_id, agent_id, agent_type, ts, seq, kind, tool, target, text, error)
    SELECT * FROM unnest($1::text[], $2::text[], $3::text[], $4::float8[], $5::int[],
                         $6::text[], $7::text[], $8::text[], $9::text[], $10::bool[])
    ON CONFLICT (session_id, agent_id, ts, seq) DO NOTHING
    RETURNING session_id, agent_id
"""

_EVENT_COLUMNS = (
    "session_id",
    "agent_id",
    "agent_type",
    "ts",
    "seq",
    "kind",
    "tool",
    "target",
    "text",
    "error",
)

_PRUNE = """
    DELETE FROM agent_session_events WHERE id IN (
        SELECT id FROM agent_session_events
        WHERE session_id = $1 AND agent_id = $2
        ORDER BY ts DESC, seq DESC
        OFFSET $3)
"""


async def ingest(
    build: Callable[[dict[str, dict[str, Any]]], list[dict[str, Any]]],
    events: list[dict[str, Any]],
) -> int:
    """Lock each session's row, build its upsert from the stored open question (which agent may
    clear it), insert the new events and prune every agent they touched. Returns how many events
    were new."""
    async with get_pool().acquire() as conn, conn.transaction():
        ids = sorted({e["session_id"] for e in events})
        stored = {r["session_id"]: dict(r) for r in await conn.fetch(_OPEN_QUESTIONS, ids)}
        # Lock and insert rows in one fixed order, so two batches over the same new sessions in
        # opposite orders cannot deadlock.
        sessions = sorted(build(stored), key=lambda s: s["session_id"])
        await conn.executemany(_UPSERT, [tuple(s[c] for c in _SESSION_COLUMNS) for s in sessions])
        columns = [[e[c] for e in events] for c in _EVENT_COLUMNS]
        inserted = await conn.fetch(_INSERT_EVENTS, *columns)
        touched = {(r["session_id"], r["agent_id"]) for r in inserted}
        if touched:
            await conn.executemany(
                _PRUNE, [(sid, agent, STEPS_PER_AGENT) for sid, agent in sorted(touched)]
            )
    return len(inserted)


async def recent(hours: int) -> list[dict[str, Any]]:
    rows = await get_pool().fetch(
        """
        SELECT * FROM agent_sessions
        WHERE last_heard_at > NOW() - make_interval(hours => $1)
        ORDER BY last_heard_at DESC
        """,
        hours,
    )
    return [dict(r) for r in rows]


async def get(session_id: str) -> dict[str, Any] | None:
    row = await get_pool().fetchrow(
        "SELECT * FROM agent_sessions WHERE session_id = $1", session_id
    )
    return dict(row) if row else None


async def events(session_id: str) -> list[dict[str, Any]]:
    rows = await get_pool().fetch(
        """
        SELECT agent_id, agent_type, ts, seq, kind, tool, target, text, error
        FROM agent_session_events WHERE session_id = $1
        ORDER BY ts, seq
        """,
        session_id,
    )
    return [dict(r) for r in rows]


async def last_heard_at() -> datetime | None:
    return await get_pool().fetchval("SELECT MAX(last_heard_at) FROM agent_sessions")


async def projects_by_workstream() -> dict[str, dict[str, str]]:
    rows = await get_pool().fetch(
        "SELECT w.id, p.slug, p.title FROM workstreams w JOIN projects p ON p.id = w.project_id"
    )
    return {str(r["id"]): {"slug": r["slug"], "title": r["title"]} for r in rows}
