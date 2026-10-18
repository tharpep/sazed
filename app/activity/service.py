"""Queries behind the Activity page: the feed, one event, a flow (linked traces), a summary."""

import json
from typing import Any

from app.db import get_pool
from app.pricing import estimate_cost_usd

# Filter categories the console offers, as kind prefixes.
CATEGORIES: dict[str, tuple[str, ...]] = {
    "chat": ("chat.",),
    "llm": ("llm",),
    "tool": ("tool",),
    "approval": ("approval",),
    "automation": ("automation",),
    "job": ("job",),
    "backlog": ("backlog",),
    "homelab": ("gw.outage", "gw.recovery", "gw.disk", "gw.deploy"),
    "notify": ("gw.notify",),
    "mcp": ("gw.mcp",),
}


def _json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def cost_usd(detail: dict[str, Any] | None) -> float | None:
    if not detail or not detail.get("model"):
        return None
    return estimate_cost_usd(
        detail["model"],
        detail.get("input_tokens") or 0,
        detail.get("output_tokens") or 0,
        detail.get("cache_read_tokens") or 0,
        detail.get("cache_write_tokens") or 0,
    )


def serialize(rec: Any, with_detail: bool = False) -> dict[str, Any]:
    d = dict(rec)
    out = {
        "id": d["id"],
        "at": d["at"].isoformat() if hasattr(d["at"], "isoformat") else d["at"],
        "kind": d["kind"],
        "title": d["title"],
        "status": d["status"],
        "trace": d.get("trace"),
        "refs": _json(d.get("refs")) or {},
        "duration_ms": d.get("duration_ms"),
    }
    detail = _json(d.get("detail"))
    if d["kind"] == "llm":
        out["cost_usd"] = cost_usd(detail)
        if detail:
            out["tokens"] = (detail.get("input_tokens") or 0) + (detail.get("output_tokens") or 0)
    if with_detail:
        out["detail"] = detail or {}
    return out


def _kind_filter(categories: list[str]) -> list[str]:
    prefixes: list[str] = []
    for c in categories:
        prefixes.extend(CATEGORIES.get(c, ()))
    return [p + "%" for p in prefixes]


async def list_events(
    categories: list[str] | None = None,
    errors_only: bool = False,
    before_id: int | None = None,
    after_id: int | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    patterns = _kind_filter(categories or [])
    rows = await get_pool().fetch(
        """
        SELECT id, at, kind, title, status, trace, refs, duration_ms,
               CASE WHEN kind = 'llm' THEN detail END AS detail
        FROM activity_events
        WHERE (cardinality($1::text[]) = 0 OR kind LIKE ANY($1::text[]))
          AND (NOT $2 OR status = 'error')
          AND ($3::bigint IS NULL OR id < $3)
          AND ($4::bigint IS NULL OR id > $4)
        ORDER BY id DESC
        LIMIT $5
        """,
        patterns,
        errors_only,
        before_id,
        after_id,
        limit,
    )
    return [serialize(r) for r in rows]


async def get_event(event_id: int) -> dict[str, Any] | None:
    rec = await get_pool().fetchrow("SELECT * FROM activity_events WHERE id = $1", event_id)
    return serialize(rec, with_detail=True) if rec else None


async def _linked(traces: set[str]) -> set[str]:
    """Traces that belong to the same flow: an automation run and its prompt session, a backlog
    item and the job it became. Walks until nothing new turns up."""
    pool = get_pool()
    seen: set[str] = set()
    todo = set(traces)
    while todo and len(seen) < 50:
        t = todo.pop()
        if t in seen:
            continue
        seen.add(t)
        kind, _, ident = t.partition(":")
        found: list[str] = []
        try:
            if kind == "run":
                sid = await pool.fetchval(
                    "SELECT session_id FROM automation_runs WHERE id = $1::uuid", ident
                )
                if sid:
                    found.append(f"session:{sid}")
            elif kind == "session":
                rid = await pool.fetchval(
                    "SELECT id FROM automation_runs WHERE session_id = $1::uuid", ident
                )
                if rid:
                    found.append(f"run:{rid}")
            elif kind == "item":
                job = await pool.fetchval(
                    "SELECT job_id FROM backlog_items WHERE id = $1::uuid", ident
                )
                if job:
                    found.append(await pool.fetchval("SELECT activity_job_trace($1)", job))
            if kind in ("job", "session", "run"):
                # Backlog items whose job sits in this flow.
                rows = await pool.fetch(
                    """
                    SELECT b.id FROM backlog_items b
                    WHERE b.job_id IS NOT NULL AND activity_job_trace(b.job_id) = $1
                    """,
                    t,
                )
                found.extend(f"item:{r['id']}" for r in rows)
        except Exception:  # a malformed id just means no links
            continue
        todo.update(f for f in found if f and f not in seen)
    return seen


async def flow(trace: str) -> dict[str, Any]:
    traces = await _linked({trace})
    pool = get_pool()
    rows = await pool.fetch(
        """
        SELECT * FROM activity_events WHERE trace = ANY($1::text[])
        ORDER BY at, id LIMIT 3000
        """,
        list(traces),
    )
    events = [serialize(r, with_detail=True) for r in rows]
    job_ids = sorted({e["refs"]["job"] for e in events if e["refs"].get("job")})
    steps: dict[str, list[dict[str, Any]]] = {}
    if job_ids:
        step_rows = await pool.fetch(
            """
            SELECT job_id, at, event FROM (
                SELECT job_id, at, event, row_number() OVER (PARTITION BY job_id ORDER BY id) AS n
                FROM job_events WHERE job_id = ANY($1::uuid[])
            ) s WHERE n <= 400 ORDER BY job_id, at
            """,
            job_ids,
        )
        for r in step_rows:
            steps.setdefault(str(r["job_id"]), []).append(
                {"at": r["at"].isoformat(), "event": _json(r["event"])}
            )
    return {"trace": trace, "traces": sorted(traces), "events": events, "steps": steps}


async def summary() -> dict[str, Any]:
    pool = get_pool()
    counts = await pool.fetch(
        """
        SELECT kind, status, count(*) AS n FROM activity_events
        WHERE at > NOW() - INTERVAL '24 hours' GROUP BY kind, status
        """
    )
    llm = await pool.fetch(
        "SELECT detail FROM activity_events WHERE kind = 'llm' AND at > NOW() - INTERVAL '24 hours'"
    )
    running = await pool.fetchrow(
        """
        SELECT
          (SELECT count(*) FROM jobs WHERE status = 'running') AS jobs_running,
          (SELECT count(*) FROM jobs WHERE status = 'queued') AS jobs_queued,
          (SELECT count(*) FROM jobs WHERE status = 'needs_approval') AS jobs_waiting,
          (SELECT count(*) FROM automation_runs WHERE status = 'running') AS runs_running,
          (SELECT count(*) FROM pending_actions WHERE status = 'pending') AS approvals_pending,
          (SELECT count(*) FROM jobs WHERE created_at > NOW() - INTERVAL '24 hours') AS jobs_24h
        """
    )
    by_kind: dict[str, int] = {}
    errors = 0
    for r in counts:
        by_kind[r["kind"]] = by_kind.get(r["kind"], 0) + r["n"]
        if r["status"] == "error":
            errors += r["n"]
    cost = 0.0
    tokens = 0
    for r in llm:
        d = _json(r["detail"]) or {}
        cost += cost_usd(d) or 0.0
        tokens += (d.get("input_tokens") or 0) + (d.get("output_tokens") or 0)
    return {
        "last_24h": {
            "chat_messages": by_kind.get("chat.user", 0),
            "llm_calls": by_kind.get("llm", 0),
            "tool_calls": by_kind.get("tool", 0),
            "jobs": running["jobs_24h"] if running else 0,
            "notifications": by_kind.get("gw.notify", 0),
            "errors": errors,
            "cost_usd": round(cost, 4),
            "tokens": tokens,
        },
        "now": {k: v for k, v in dict(running).items() if k != "jobs_24h"} if running else {},
    }
