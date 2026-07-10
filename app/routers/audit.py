"""Audit log — action history for all tool calls, plus LLM cost/token rollups."""

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Query

from app.db import get_pool
from app.pricing import estimate_cost_usd

router = APIRouter()


@router.get("/actions")
async def list_action_logs(
    session_id: str | None = Query(None),
    status: str | None = Query(None),
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
):
    pool = get_pool()
    filters: list[str] = []
    params: list = []

    if session_id:
        params.append(uuid.UUID(session_id))
        filters.append(f"session_id = ${len(params)}")
    if status:
        params.append(status)
        filters.append(f"status = ${len(params)}")

    where = f"WHERE {' AND '.join(filters)}" if filters else ""
    params += [limit, offset]

    rows = await pool.fetch(
        f"""
        SELECT id, session_id, timestamp, tool_name, input, output,
               status, error_message, duration_ms
        FROM action_logs {where}
        ORDER BY timestamp DESC
        LIMIT ${len(params) - 1} OFFSET ${len(params)}
        """,
        *params,
    )

    return [
        {
            "id": str(r["id"]),
            "session_id": str(r["session_id"]) if r["session_id"] else None,
            "timestamp": r["timestamp"].isoformat(),
            "tool_name": r["tool_name"],
            "input": r["input"],
            "output": r["output"],
            "status": r["status"],
            "error_message": r["error_message"],
            "duration_ms": r["duration_ms"],
        }
        for r in rows
    ]


def _percentile(sorted_data: list[int], pct: float) -> int | None:
    """Nearest-rank percentile over an already-sorted list. None if empty."""
    if not sorted_data:
        return None
    idx = min(len(sorted_data) - 1, int(len(sorted_data) * pct))
    return sorted_data[idx]


@router.get("/metrics")
async def get_metrics(since_hours: int = Query(24, ge=1, le=24 * 30)):
    """Rollup metrics: LLM token/cost by model and purpose, tool success rate and latency."""
    pool = get_pool()
    since = datetime.now(UTC) - timedelta(hours=since_hours)

    llm_rows = await pool.fetch(
        "SELECT model, purpose, input_tokens, output_tokens, "
        "cache_read_tokens, cache_write_tokens "
        "FROM llm_calls WHERE timestamp >= $1",
        since,
    )

    by_model: dict[str, dict[str, float]] = {}
    by_purpose: dict[str, dict[str, float]] = {}
    total_cost = 0.0
    total_input = total_output = 0

    for r in llm_rows:
        cost = (
            estimate_cost_usd(
                r["model"],
                r["input_tokens"] or 0,
                r["output_tokens"] or 0,
                r["cache_read_tokens"] or 0,
                r["cache_write_tokens"] or 0,
            )
            or 0.0
        )
        total_cost += cost
        total_input += r["input_tokens"] or 0
        total_output += r["output_tokens"] or 0

        m = by_model.setdefault(
            r["model"], {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
        )
        m["calls"] += 1
        m["input_tokens"] += r["input_tokens"] or 0
        m["output_tokens"] += r["output_tokens"] or 0
        m["cost_usd"] += cost

        p = by_purpose.setdefault(r["purpose"] or "unknown", {"calls": 0, "cost_usd": 0.0})
        p["calls"] += 1
        p["cost_usd"] += cost

    for m in by_model.values():
        m["cost_usd"] = round(m["cost_usd"], 4)
    for p in by_purpose.values():
        p["cost_usd"] = round(p["cost_usd"], 4)

    tool_rows = await pool.fetch(
        "SELECT tool_name, status, duration_ms FROM action_logs WHERE timestamp >= $1",
        since,
    )
    total_tools = len(tool_rows)
    success_tools = sum(1 for r in tool_rows if r["status"] == "success")
    durations = sorted(r["duration_ms"] for r in tool_rows if r["duration_ms"] is not None)

    call_counts: dict[str, int] = {}
    error_counts: dict[str, int] = {}
    for r in tool_rows:
        call_counts[r["tool_name"]] = call_counts.get(r["tool_name"], 0) + 1
        if r["status"] == "error":
            error_counts[r["tool_name"]] = error_counts.get(r["tool_name"], 0) + 1

    top_by_calls = sorted(call_counts.items(), key=lambda kv: kv[1], reverse=True)[:10]
    top_by_errors = sorted(error_counts.items(), key=lambda kv: kv[1], reverse=True)[:10]

    return {
        "since": since.isoformat(),
        "llm": {
            "total_calls": len(llm_rows),
            "total_input_tokens": total_input,
            "total_output_tokens": total_output,
            "total_cost_usd": round(total_cost, 4),
            "by_model": by_model,
            "by_purpose": by_purpose,
        },
        "tools": {
            "total_calls": total_tools,
            "success_rate": (success_tools / total_tools) if total_tools else None,
            "duration_ms_p50": _percentile(durations, 0.50),
            "duration_ms_p95": _percentile(durations, 0.95),
            "top_by_call_count": [{"tool_name": k, "count": v} for k, v in top_by_calls],
            "top_by_error_count": [{"tool_name": k, "count": v} for k, v in top_by_errors],
        },
    }


@router.get("/sessions/{session_id}/cost")
async def get_session_cost(session_id: str):
    """Per-session token and dollar total, joining llm_calls and action_logs."""
    pool = get_pool()
    sid = uuid.UUID(session_id)

    llm_rows = await pool.fetch(
        "SELECT model, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens "
        "FROM llm_calls WHERE session_id = $1",
        sid,
    )
    total_cost = 0.0
    total_input = total_output = 0
    for r in llm_rows:
        cost = (
            estimate_cost_usd(
                r["model"],
                r["input_tokens"] or 0,
                r["output_tokens"] or 0,
                r["cache_read_tokens"] or 0,
                r["cache_write_tokens"] or 0,
            )
            or 0.0
        )
        total_cost += cost
        total_input += r["input_tokens"] or 0
        total_output += r["output_tokens"] or 0

    tool_count = await pool.fetchval("SELECT COUNT(*) FROM action_logs WHERE session_id = $1", sid)

    return {
        "session_id": session_id,
        "llm_calls": len(llm_rows),
        "total_input_tokens": total_input,
        "total_output_tokens": total_output,
        "total_cost_usd": round(total_cost, 4),
        "tool_calls": tool_count,
    }
