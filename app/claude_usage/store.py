"""Postgres access for the claude_usage_* tables (migration 025)."""

import json
from datetime import date, datetime, timedelta
from typing import Any

from app.db import get_pool
from app.pricing import LONG_PROMPT_TOKENS

MAX_ROWS = 5000

_RANGE = "ts >= $1 AND ts <= $2 AND ($3::text IS NULL OR machine = $3)"
_PROMPT_RANGE = "p.ts >= $1 AND p.ts <= $2 AND ($3::text IS NULL OR p.machine = $3)"
_BASH = "name = 'Bash'"
_TOKEN_SUMS = (
    "count(*)::int AS calls, sum(input_tokens)::bigint AS input_tokens, "
    "sum(output_tokens)::bigint AS output_tokens, "
    "sum(cache_read_tokens)::bigint AS cache_read_tokens, "
    "sum(cache_write_tokens)::bigint AS cache_write_tokens"
)
_PRICE_KEYS = (
    "model, sidechain, COALESCE(speed = 'fast', FALSE) AS fast, "
    "COALESCE(inference_geo = 'us', FALSE) AS us_only, "
    f"context > {LONG_PROMPT_TOKENS} AS long_prompt"
)
_RESPONSE_SUMS = (
    f"{_TOKEN_SUMS}, sum(cache_write_1h_tokens)::bigint AS cache_write_1h_tokens, "
    "sum(web_search_requests)::int AS web_searches"
)

_TABLES: dict[str, tuple[str, list[tuple[str, str]]]] = {
    "prompts": (
        "claude_usage_prompts",
        [
            ("uuid", "text"),
            ("session_id", "text"),
            ("ts", "timestamptz"),
            ("chars", "integer"),
            ("source", "text"),
            ("permission_mode", "text"),
            ("confused", "boolean"),
        ],
    ),
    "responses": (
        "claude_usage_responses",
        [
            ("uuid", "text"),
            ("session_id", "text"),
            ("ts", "timestamptz"),
            ("model", "text"),
            ("effort", "text"),
            ("output_style", "text"),
            ("input_tokens", "bigint"),
            ("output_tokens", "bigint"),
            ("cache_read_tokens", "bigint"),
            ("cache_write_tokens", "bigint"),
            ("sidechain", "boolean"),
            ("cache_write_1h_tokens", "bigint"),
            ("speed", "text"),
            ("inference_geo", "text"),
            ("web_search_requests", "integer"),
            ("api_request_id", "text"),
        ],
    ),
    "tool_calls": (
        "claude_usage_tool_calls",
        [
            ("tool_use_id", "text"),
            ("session_id", "text"),
            ("ts", "timestamptz"),
            ("name", "text"),
            ("detail", "text"),
            ("writes_files", "boolean"),
            ("sidechain", "boolean"),
        ],
    ),
    "events": (
        "claude_usage_events",
        [("uuid", "text"), ("session_id", "text"), ("ts", "timestamptz"), ("kind", "text")],
    ),
    "slash_commands": (
        "claude_usage_slash_commands",
        [("ts", "timestamptz"), ("project", "text"), ("command", "text")],
    ),
}

_SESSION_COLUMNS = [
    ("session_id", "text"),
    ("project", "text"),
    ("title", "text"),
    ("agent_name", "text"),
    ("version", "text"),
    ("first_ts", "timestamptz"),
    ("last_ts", "timestamptz"),
]

_UPSERT_SESSIONS = """
INSERT INTO claude_usage_sessions
    (machine, session_id, project, title, agent_name, version, first_ts, last_ts)
SELECT $1::text, u.* FROM unnest(
    $2::text[], $3::text[], $4::text[], $5::text[], $6::text[], $7::timestamptz[], $8::timestamptz[]
) AS u(session_id, project, title, agent_name, version, first_ts, last_ts)
ON CONFLICT (machine, session_id) DO UPDATE SET
    project = COALESCE(claude_usage_sessions.project, EXCLUDED.project),
    title = COALESCE(claude_usage_sessions.title, EXCLUDED.title),
    agent_name = COALESCE(claude_usage_sessions.agent_name, EXCLUDED.agent_name),
    version = COALESCE(claude_usage_sessions.version, EXCLUDED.version),
    first_ts = LEAST(claude_usage_sessions.first_ts, EXCLUDED.first_ts),
    last_ts = GREATEST(claude_usage_sessions.last_ts, EXCLUDED.last_ts)
RETURNING (xmax = 0) AS inserted
"""


def merge_sessions(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse repeats of one session_id by the stored-row merge rule."""
    merged: dict[str, dict[str, Any]] = {}
    for row in rows:
        have = merged.get(row["session_id"])
        if have is None:
            merged[row["session_id"]] = dict(row)
            continue
        for field in ("project", "title", "agent_name", "version"):
            if have[field] is None:
                have[field] = row[field]
        have["first_ts"] = min(have["first_ts"], row["first_ts"])
        have["last_ts"] = max(have["last_ts"], row["last_ts"])
    return list(merged.values())


def _insert_sql(table: str, columns: list[tuple[str, str]]) -> str:
    names = ", ".join(name for name, _ in columns)
    arrays = ", ".join(f"${i}::{kind}[]" for i, (_, kind) in enumerate(columns, start=2))
    return (
        f"INSERT INTO {table} (machine, {names}) SELECT $1::text, u.* "
        f"FROM unnest({arrays}) AS u({names}) ON CONFLICT DO NOTHING RETURNING 1"
    )


def _columns(rows: list[dict[str, Any]], columns: list[tuple[str, str]]) -> list[list[Any]]:
    return [[row[name] for row in rows] for name, _ in columns]


async def ingest(machine: str, batch: dict[str, list[dict[str, Any]]]) -> dict[str, int]:
    """Insert one batch in a single transaction and count the rows that were new."""
    accepted: dict[str, int] = {}
    async with get_pool().acquire() as conn, conn.transaction():
        sessions = merge_sessions(batch.get("sessions", []))
        if sessions:
            rows = await conn.fetch(
                _UPSERT_SESSIONS, machine, *_columns(sessions, _SESSION_COLUMNS)
            )
            accepted["sessions"] = sum(1 for r in rows if r["inserted"])
        else:
            accepted["sessions"] = 0
        for key, (table, columns) in _TABLES.items():
            rows = batch.get(key, [])
            if not rows:
                accepted[key] = 0
                continue
            result = await conn.fetch(
                _insert_sql(table, columns), machine, *_columns(rows, columns)
            )
            accepted[key] = len(result)
    return accepted


_API_REQUEST_COLUMNS = [
    ("request_id", "text"),
    ("machine", "text"),
    ("session_id", "text"),
    ("ts", "timestamptz"),
    ("model", "text"),
    ("query_source", "text"),
    ("speed", "text"),
    ("effort", "text"),
    ("input_tokens", "bigint"),
    ("output_tokens", "bigint"),
    ("cache_read_tokens", "bigint"),
    ("cache_write_tokens", "bigint"),
    ("cache_write_1h_tokens", "bigint"),
    ("cost_usd", "numeric"),
]
_UNSEEN = (
    "NOT EXISTS (SELECT 1 FROM claude_usage_responses r WHERE r.api_request_id = t.request_id)"
)
_SIDE_SUMS = f"{_TOKEN_SUMS}, sum(cost_usd)::float8 AS cost_usd"


async def ingest_api_requests(rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    names = ", ".join(name for name, _ in _API_REQUEST_COLUMNS)
    arrays = ", ".join(f"${i}::{kind}[]" for i, (_, kind) in enumerate(_API_REQUEST_COLUMNS, 1))
    result = await get_pool().fetch(
        f"INSERT INTO claude_usage_api_requests ({names}) SELECT * FROM unnest({arrays}) "
        f"ON CONFLICT DO NOTHING RETURNING 1",
        *_columns(rows, _API_REQUEST_COLUMNS),
    )
    return len(result)


async def overview_rows(frm: datetime, to: datetime, machine: str | None) -> dict[str, Any]:
    args = (frm, to, machine)
    pool = get_pool()
    day = "(ts AT TIME ZONE 'UTC')::date"
    return {
        "machines": [
            r["machine"]
            for r in await pool.fetch(
                "SELECT DISTINCT machine FROM claude_usage_sessions ORDER BY machine"
            )
        ],
        "prompt_days": await pool.fetch(
            f"SELECT machine, session_id, {day} AS day, count(*)::int AS prompts, "
            f"(count(*) FILTER (WHERE confused))::int AS confused "
            f"FROM claude_usage_prompts WHERE {_RANGE} GROUP BY 1, 2, 3",
            *args,
        ),
        "peaks": [
            (r["peak"], r["prompts"])
            for r in await pool.fetch(
                f"SELECT max(context) AS peak, ("
                f"SELECT count(*)::int FROM claude_usage_prompts p WHERE p.machine = r.machine "
                f"AND p.session_id = r.session_id AND {_PROMPT_RANGE}) AS prompts "
                f"FROM claude_usage_responses r "
                f"WHERE NOT sidechain AND {_RANGE} AND EXISTS ("
                f"SELECT 1 FROM claude_usage_prompts p WHERE p.machine = r.machine "
                f"AND p.session_id = r.session_id AND {_PROMPT_RANGE}) "
                f"GROUP BY machine, session_id",
                *args,
            )
        ],
        "tool_days": await pool.fetch(
            f"SELECT {day} AS day, count(*)::int AS calls, "
            f"(count(*) FILTER (WHERE writes_files AND {_BASH}))::int AS bash_writes, "
            f"(count(*) FILTER (WHERE writes_files AND NOT ({_BASH})))::int AS edit_writes "
            f"FROM claude_usage_tool_calls WHERE {_RANGE} GROUP BY 1",
            *args,
        ),
        "spend": await pool.fetch(
            f"SELECT {day} AS day, {_PRICE_KEYS}, {_RESPONSE_SUMS} "
            f"FROM claude_usage_responses WHERE {_RANGE} GROUP BY 1, 2, 3, 4, 5, 6",
            *args,
        ),
        "sazed_spend": [] if machine else await sazed_spend_rows(pool, frm, to),
        "side_spend": await pool.fetch(
            f"SELECT {day} AS day, 'side_calls' AS source, model, query_source, {_SIDE_SUMS} "
            f"FROM claude_usage_api_requests t WHERE {_UNSEEN} AND {_RANGE} GROUP BY 1, 2, 3, 4",
            *args,
        ),
        "models": await pool.fetch(
            f"SELECT model, count(*)::int AS n FROM claude_usage_responses "
            f"WHERE model IS NOT NULL AND {_RANGE} GROUP BY model ORDER BY n DESC, model",
            *args,
        ),
        "skills": await _named_counts(
            pool, "name = 'Skill'", "detail", "claude_usage_tool_calls", args
        ),
        "subagents": await _named_counts(
            pool, "name IN ('Agent', 'Task')", "detail", "claude_usage_tool_calls", args
        ),
        "slash_commands": await _named_counts(
            pool, "TRUE", "command", "claude_usage_slash_commands", args
        ),
        "styles": await pool.fetch(
            f"SELECT r.output_style AS style, count(*)::int AS prompts, "
            f"(count(*) FILTER (WHERE p.confused))::int AS confused "
            f"FROM claude_usage_prompts p JOIN LATERAL ("
            f"SELECT output_style FROM claude_usage_responses x WHERE x.machine = p.machine "
            f"AND x.session_id = p.session_id AND NOT x.sidechain AND x.ts < p.ts "
            f"ORDER BY x.ts DESC LIMIT 1) r ON TRUE "
            f"WHERE r.output_style IS NOT NULL AND {_PROMPT_RANGE} "
            f"GROUP BY 1 ORDER BY prompts DESC, style",
            *args,
        ),
    }


async def sazed_spend_rows(pool, frm: datetime, to: datetime) -> list[dict[str, Any]]:
    """Sazed's own model calls and worker jobs, the spend no Claude Code transcript records."""
    calls = await pool.fetch(
        f"SELECT (timestamp AT TIME ZONE 'UTC')::date AS day, 'sazed_api' AS source, model, "
        f"(COALESCE(input_tokens, 0) + COALESCE(cache_read_tokens, 0) "
        f"+ COALESCE(cache_write_tokens, 0)) > {LONG_PROMPT_TOKENS} AS long_prompt, "
        f"count(*)::int AS calls, sum(COALESCE(input_tokens, 0))::bigint AS input_tokens, "
        f"sum(COALESCE(output_tokens, 0))::bigint AS output_tokens, "
        f"sum(COALESCE(cache_read_tokens, 0))::bigint AS cache_read_tokens, "
        f"sum(COALESCE(cache_write_tokens, 0))::bigint AS cache_write_tokens "
        f"FROM llm_calls WHERE timestamp >= $1 AND timestamp <= $2 GROUP BY 1, 2, 3, 4",
        frm,
        to,
    )
    usage = "COALESCE((meta->'usage'->>'{}')::bigint, 0)"
    prompt = " + ".join(
        usage.format(k)
        for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    )
    jobs = await pool.fetch(
        f"SELECT (finished_at AT TIME ZONE 'UTC')::date AS day, 'sazed_jobs' AS source, model, "
        f"({prompt}) > {LONG_PROMPT_TOKENS} AS long_prompt, count(*)::int AS calls, "
        f"sum({usage.format('input_tokens')})::bigint AS input_tokens, "
        f"sum({usage.format('output_tokens')})::bigint AS output_tokens, "
        f"sum({usage.format('cache_read_input_tokens')})::bigint AS cache_read_tokens, "
        f"sum({usage.format('cache_creation_input_tokens')})::bigint AS cache_write_tokens "
        f"FROM jobs WHERE jsonb_typeof(meta->'usage') = 'object' "
        f"AND finished_at >= $1 AND finished_at <= $2 GROUP BY 1, 2, 3, 4",
        frm,
        to,
    )
    return [*calls, *jobs]


async def _named_counts(pool, where: str, column: str, table: str, args: tuple) -> list:
    return await pool.fetch(
        f"SELECT {column} AS name, count(*)::int AS n FROM {table} "
        f"WHERE {column} IS NOT NULL AND {where} AND {_RANGE} GROUP BY 1 ORDER BY n DESC, 1",
        *args,
    )


async def session_rows(
    frm: datetime, to: datetime, machine: str | None, min_prompts: int
) -> dict[str, Any]:
    args = (frm, to, machine)
    pool = get_pool()
    sessions = await pool.fetch(
        f"SELECT s.machine, s.session_id, s.project, s.title, s.first_ts, s.last_ts, p.prompts "
        f"FROM claude_usage_sessions s JOIN ("
        f"SELECT machine, session_id, count(*)::int AS prompts FROM claude_usage_prompts "
        f"WHERE {_RANGE} GROUP BY 1, 2 HAVING count(*) >= $4) p "
        f"ON p.machine = s.machine AND p.session_id = s.session_id "
        f"ORDER BY s.first_ts DESC",
        *args,
        min_prompts,
    )
    if not sessions:
        return {
            "sessions": [],
            "responses": [],
            "spend": [],
            "side_spend": [],
            "counts": [],
            "confused": [],
        }
    keys = ([r["machine"] for r in sessions], [r["session_id"] for r in sessions])
    scope = (
        "JOIN unnest($3::text[], $4::text[]) AS k(machine, session_id) "
        "ON k.machine = t.machine AND k.session_id = t.session_id"
    )
    return {
        "sessions": sessions,
        "responses": await pool.fetch(
            f"SELECT t.machine, t.session_id, t.ts, t.context, t.model "
            f"FROM claude_usage_responses t {scope} "
            f"WHERE NOT t.sidechain AND t.ts >= $1 AND t.ts <= $2 ORDER BY t.ts",
            frm,
            to,
            *keys,
        ),
        "spend": await pool.fetch(
            f"SELECT t.machine, t.session_id, {_PRICE_KEYS}, {_RESPONSE_SUMS} "
            f"FROM claude_usage_responses t {scope} "
            f"WHERE t.ts >= $1 AND t.ts <= $2 GROUP BY 1, 2, 3, 4, 5, 6, 7",
            frm,
            to,
            *keys,
        ),
        "side_spend": await pool.fetch(
            f"SELECT t.machine, t.session_id, 'side_calls' AS source, t.model, {_SIDE_SUMS} "
            f"FROM claude_usage_api_requests t {scope} "
            f"WHERE {_UNSEEN} AND t.ts >= $1 AND t.ts <= $2 GROUP BY 1, 2, 3, 4",
            frm,
            to,
            *keys,
        ),
        "counts": await pool.fetch(
            f"SELECT t.machine, t.session_id, 'subagents' AS kind, count(*)::int AS n "
            f"FROM claude_usage_tool_calls t {scope} "
            f"WHERE t.name IN ('Agent', 'Task') AND t.ts >= $1 AND t.ts <= $2 GROUP BY 1, 2 "
            f"UNION ALL "
            f"SELECT t.machine, t.session_id, t.kind, count(*)::int "
            f"FROM claude_usage_events t {scope} "
            f"WHERE t.ts >= $1 AND t.ts <= $2 GROUP BY 1, 2, 3",
            frm,
            to,
            *keys,
        ),
        "confused": await pool.fetch(
            f"SELECT t.machine, t.session_id, t.ts FROM claude_usage_prompts t {scope} "
            f"WHERE t.confused AND t.ts >= $1 AND t.ts <= $2 ORDER BY t.ts",
            frm,
            to,
            *keys,
        ),
    }


_JOB_USAGE = "COALESCE((meta->'usage'->>'{}')::bigint, 0)"


def _job_tokens(*keys: str) -> str:
    return " + ".join(_JOB_USAGE.format(k) for k in keys)


async def hub_rows(frm: datetime, to: datetime, tz: str) -> dict[str, Any]:
    """Every usage source by local day for the usage hub, plus feed freshness and plan readings."""
    pool = get_pool()
    day = "(ts AT TIME ZONE $3)::date"
    rng = "ts >= $1 AND ts <= $2"
    prompt = _job_tokens("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    return {
        "claude_code": await pool.fetch(
            f"SELECT {day} AS day, machine, {_PRICE_KEYS}, {_RESPONSE_SUMS} "
            f"FROM claude_usage_responses WHERE {rng} GROUP BY 1, 2, 3, 4, 5, 6, 7",
            frm,
            to,
            tz,
        ),
        "side_calls": await pool.fetch(
            f"SELECT {day} AS day, machine, model, {_SIDE_SUMS} "
            f"FROM claude_usage_api_requests t WHERE {_UNSEEN} AND {rng} GROUP BY 1, 2, 3",
            frm,
            to,
            tz,
        ),
        "sazed_api": await pool.fetch(
            f"SELECT (timestamp AT TIME ZONE $3)::date AS day, model, "
            f"(COALESCE(input_tokens, 0) + COALESCE(cache_read_tokens, 0) "
            f"+ COALESCE(cache_write_tokens, 0)) > {LONG_PROMPT_TOKENS} AS long_prompt, "
            f"count(*)::int AS calls, sum(COALESCE(input_tokens, 0))::bigint AS input_tokens, "
            f"sum(COALESCE(output_tokens, 0))::bigint AS output_tokens, "
            f"sum(COALESCE(cache_read_tokens, 0))::bigint AS cache_read_tokens, "
            f"sum(COALESCE(cache_write_tokens, 0))::bigint AS cache_write_tokens "
            f"FROM llm_calls WHERE timestamp >= $1 AND timestamp <= $2 GROUP BY 1, 2, 3",
            frm,
            to,
            tz,
        ),
        # Claude jobs report cache_*_input_tokens; agy jobs report cache_read_tokens and
        # thinking_tokens, so each sum reads both spellings.
        "jobs": await pool.fetch(
            f"SELECT (finished_at AT TIME ZONE $3)::date AS day, agent, model, "
            f"({prompt}) > {LONG_PROMPT_TOKENS} AS long_prompt, count(*)::int AS calls, "
            f"sum({_job_tokens('input_tokens')})::bigint AS input_tokens, "
            f"sum({_job_tokens('output_tokens')})::bigint AS output_tokens, "
            f"sum({_job_tokens('cache_read_input_tokens', 'cache_read_tokens')})::bigint "
            f"AS cache_read_tokens, "
            f"sum({_job_tokens('cache_creation_input_tokens')})::bigint AS cache_write_tokens, "
            f"sum({_job_tokens('thinking_tokens')})::bigint AS thinking_tokens "
            f"FROM jobs WHERE jsonb_typeof(meta->'usage') = 'object' "
            f"AND finished_at >= $1 AND finished_at <= $2 GROUP BY 1, 2, 3, 4",
            frm,
            to,
            tz,
        ),
        "fresh": await pool.fetch(
            "SELECT 'claude_code' AS source, machine, max(ts) AS last_at "
            "FROM claude_usage_responses GROUP BY machine "
            "UNION ALL SELECT 'sazed_api', NULL, max(timestamp) FROM llm_calls "
            "UNION ALL SELECT 'worker_' || agent, NULL, max(finished_at) FROM jobs "
            "WHERE jsonb_typeof(meta->'usage') = 'object' GROUP BY agent"
        ),
        "readings": await pool.fetch(
            "SELECT plan_window, read_at, used_pct, resets_at, status, source, machine "
            "FROM plan_limit_readings WHERE read_at >= $1 OR id IN ("
            "SELECT DISTINCT ON (plan_window) id FROM plan_limit_readings "
            "ORDER BY plan_window, read_at DESC) ORDER BY read_at",
            to - timedelta(days=8),
        ),
        "plans": await plan_periods(),
    }


async def add_reading(info: dict[str, Any], read_at: datetime, source: str, machine: str) -> int:
    """Both plan windows from one rate_limit_info object; returns how many windows were new."""
    return await get_pool().fetchval(
        "SELECT plan_limit_readings_insert($1::jsonb, $2, $3, $4, NULL, NULL)",
        json.dumps(info),
        read_at,
        source,
        machine,
    )


async def touch_activity(machine: str, at: datetime) -> None:
    await get_pool().execute(
        "INSERT INTO claude_activity (machine, last_at) VALUES ($1, $2) "
        "ON CONFLICT (machine) DO UPDATE SET last_at = GREATEST(claude_activity.last_at, $2)",
        machine,
        at,
    )


async def probe_state() -> dict[str, Any]:
    row = await get_pool().fetchrow(
        "SELECT (SELECT max(read_at) FROM plan_limit_readings) AS last_read, "
        "(SELECT resets_at FROM plan_limit_readings WHERE plan_window = 'five_hour' "
        "ORDER BY read_at DESC LIMIT 1) AS five_hour_resets, "
        "(SELECT max(last_at) FROM claude_activity) AS last_active"
    )
    return dict(row)


async def plan_periods() -> list[dict[str, Any]]:
    rows = await get_pool().fetch(
        "SELECT starts_on, plan, monthly_usd FROM claude_plan_periods ORDER BY starts_on"
    )
    return [dict(r) for r in rows]


async def set_plan_period(starts_on: date, plan: str, monthly_usd: float) -> None:
    await get_pool().execute(
        "INSERT INTO claude_plan_periods (starts_on, plan, monthly_usd) VALUES ($1, $2, $3) "
        "ON CONFLICT (starts_on) DO UPDATE SET plan = $2, monthly_usd = $3, updated_at = NOW()",
        starts_on,
        plan,
        monthly_usd,
    )
