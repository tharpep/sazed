"""SQL for Claude Code sessions and their steps (migration 039)."""

from datetime import datetime
from typing import Any

from app.db import get_pool

STEPS_PER_AGENT = 80

_UPSERT = """
    INSERT INTO agent_sessions AS s (
        session_id, host, cwd, repo, branch, card_id, started_at,
        state, state_ts, state_seq, ended_at,
        doing, doing_source, doing_ts, doing_seq,
        question, question_ts, question_seq, cleared_ts, cleared_seq)
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18,
            $19, $20)
    ON CONFLICT (session_id) DO UPDATE SET
        host = CASE WHEN (EXCLUDED.state_ts, EXCLUDED.state_seq) >= (s.state_ts, s.state_seq)
                    THEN COALESCE(EXCLUDED.host, s.host) ELSE COALESCE(s.host, EXCLUDED.host) END,
        cwd = CASE WHEN (EXCLUDED.state_ts, EXCLUDED.state_seq) >= (s.state_ts, s.state_seq)
                   THEN COALESCE(EXCLUDED.cwd, s.cwd) ELSE COALESCE(s.cwd, EXCLUDED.cwd) END,
        repo = CASE WHEN (EXCLUDED.state_ts, EXCLUDED.state_seq) >= (s.state_ts, s.state_seq)
                    THEN COALESCE(EXCLUDED.repo, s.repo) ELSE COALESCE(s.repo, EXCLUDED.repo) END,
        branch = CASE WHEN (EXCLUDED.state_ts, EXCLUDED.state_seq) >= (s.state_ts, s.state_seq)
                      THEN COALESCE(EXCLUDED.branch, s.branch)
                      ELSE COALESCE(s.branch, EXCLUDED.branch) END,
        card_id = CASE WHEN (EXCLUDED.state_ts, EXCLUDED.state_seq) >= (s.state_ts, s.state_seq)
                       THEN COALESCE(EXCLUDED.card_id, s.card_id)
                       ELSE COALESCE(s.card_id, EXCLUDED.card_id) END,
        started_at = LEAST(s.started_at, EXCLUDED.started_at),
        last_heard_at = NOW(),
        state = CASE WHEN (EXCLUDED.state_ts, EXCLUDED.state_seq) > (s.state_ts, s.state_seq)
                     THEN EXCLUDED.state ELSE s.state END,
        ended_at = CASE WHEN (EXCLUDED.state_ts, EXCLUDED.state_seq) > (s.state_ts, s.state_seq)
                        THEN EXCLUDED.ended_at ELSE s.ended_at END,
        state_seq = CASE WHEN (EXCLUDED.state_ts, EXCLUDED.state_seq) > (s.state_ts, s.state_seq)
                         THEN EXCLUDED.state_seq ELSE s.state_seq END,
        state_ts = GREATEST(EXCLUDED.state_ts, s.state_ts),
        doing = CASE WHEN (EXCLUDED.doing_ts, EXCLUDED.doing_seq)
                          > (COALESCE(s.doing_ts, -1), COALESCE(s.doing_seq, -1))
                     THEN EXCLUDED.doing ELSE s.doing END,
        doing_source = CASE WHEN (EXCLUDED.doing_ts, EXCLUDED.doing_seq)
                                 > (COALESCE(s.doing_ts, -1), COALESCE(s.doing_seq, -1))
                            THEN EXCLUDED.doing_source ELSE s.doing_source END,
        doing_seq = CASE WHEN (EXCLUDED.doing_ts, EXCLUDED.doing_seq)
                              > (COALESCE(s.doing_ts, -1), COALESCE(s.doing_seq, -1))
                         THEN EXCLUDED.doing_seq ELSE s.doing_seq END,
        doing_ts = CASE WHEN (EXCLUDED.doing_ts, EXCLUDED.doing_seq)
                             > (COALESCE(s.doing_ts, -1), COALESCE(s.doing_seq, -1))
                        THEN EXCLUDED.doing_ts ELSE s.doing_ts END,
        question = CASE WHEN (EXCLUDED.question_ts, EXCLUDED.question_seq)
                             > (COALESCE(s.question_ts, -1), COALESCE(s.question_seq, -1))
                        THEN EXCLUDED.question ELSE s.question END,
        question_seq = CASE WHEN (EXCLUDED.question_ts, EXCLUDED.question_seq)
                                 > (COALESCE(s.question_ts, -1), COALESCE(s.question_seq, -1))
                            THEN EXCLUDED.question_seq ELSE s.question_seq END,
        question_ts = CASE WHEN (EXCLUDED.question_ts, EXCLUDED.question_seq)
                                > (COALESCE(s.question_ts, -1), COALESCE(s.question_seq, -1))
                           THEN EXCLUDED.question_ts ELSE s.question_ts END,
        cleared_seq = CASE WHEN (EXCLUDED.cleared_ts, EXCLUDED.cleared_seq)
                                > (COALESCE(s.cleared_ts, -1), COALESCE(s.cleared_seq, -1))
                           THEN EXCLUDED.cleared_seq ELSE s.cleared_seq END,
        cleared_ts = CASE WHEN (EXCLUDED.cleared_ts, EXCLUDED.cleared_seq)
                               > (COALESCE(s.cleared_ts, -1), COALESCE(s.cleared_seq, -1))
                          THEN EXCLUDED.cleared_ts ELSE s.cleared_ts END
"""

_SESSION_COLUMNS = (
    "session_id",
    "host",
    "cwd",
    "repo",
    "branch",
    "card_id",
    "started_at",
    "state",
    "state_ts",
    "state_seq",
    "ended_at",
    "doing",
    "doing_source",
    "doing_ts",
    "doing_seq",
    "question",
    "question_ts",
    "question_seq",
    "cleared_ts",
    "cleared_seq",
)

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


async def ingest(sessions: list[dict[str, Any]], events: list[dict[str, Any]]) -> int:
    """Upsert each session, insert its new events and prune every agent they touched. Returns
    how many events were new."""
    async with get_pool().acquire() as conn, conn.transaction():
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
