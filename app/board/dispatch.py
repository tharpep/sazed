"""Dispatch plans and their board rows, posted by sazed-worker's dispatch."""

import json
from datetime import datetime
from typing import Any

from app.board import service
from app.db import get_pool

NEEDS_STATES = ("waiting", "failed", "conflict", "blocked")
NEEDS_WINDOW_S = 24 * 3600
STEP_TEXT_MAX = 300
PLAN_STEPS_MAX = 80
_NEXT = {
    "waiting": "Answer its question in the plan",
    "failed": "Fix the plan and run it again",
    "conflict": "Fix the plan and run it again",
    "blocked": "Fix the plan and run it again",
}


def _json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


async def upsert_plan(plan: str, repo: str, base: str, host: str, tasks: list[dict]) -> None:
    await get_pool().execute(
        """
        INSERT INTO dispatch_plans (plan, repo, base, host, tasks)
        VALUES ($1, $2, $3, $4, $5::jsonb)
        ON CONFLICT (plan) DO UPDATE
        SET repo = $2, base = $3, host = $4, tasks = $5::jsonb, updated_at = NOW()
        """,
        plan,
        repo,
        base,
        host,
        json.dumps(tasks),
    )


async def insert_row(
    plan: str,
    task: str,
    ts: float,
    state: str,
    line: str,
    ref: str | None,
    cost_usd: float | None,
    session: str | None,
    meta: dict,
) -> None:
    await get_pool().execute(
        """
        INSERT INTO dispatch_rows (plan, task, ts, state, line, ref, cost_usd, session, meta)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb)
        ON CONFLICT (plan, task, ts, state) DO NOTHING
        """,
        plan,
        task,
        ts,
        state,
        line,
        ref,
        cost_usd,
        session,
        json.dumps(meta),
    )


async def insert_steps(plan: str, task: str, steps: list[dict]) -> None:
    await get_pool().executemany(
        """
        INSERT INTO dispatch_steps (plan, task, ts, seq, kind, tool, text, error)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        ON CONFLICT (plan, task, ts, seq) DO NOTHING
        """,
        [
            (
                plan,
                task,
                s["ts"],
                s["seq"],
                s["kind"],
                s["tool"],
                s["text"][:STEP_TEXT_MAX],
                s["error"],
            )
            for s in steps
        ],
    )


def run_start(rows: list[dict[str, Any]]) -> float | None:
    """ts of the first row after the last cleaned row; 0 with none; None if nothing follows."""
    start = 0.0
    for i, r in enumerate(rows):
        if r["state"] == "cleaned":
            if i + 1 == len(rows):
                return None
            start = rows[i + 1]["ts"]
    return start


async def steps_since(starts: dict[tuple[str, str], float]) -> dict[tuple[str, str], list[dict]]:
    """Each (plan, task)'s steps with ts at or after its start, in ts and seq order."""
    if not starts:
        return {}
    keys = list(starts)
    recs = await get_pool().fetch(
        """
        SELECT s.plan, s.task, s.ts, s.seq, s.kind, s.tool, s.text, s.error, s.received_at
        FROM dispatch_steps s
        JOIN unnest($1::text[], $2::text[], $3::float8[]) AS k(plan, task, start)
          ON s.plan = k.plan AND s.task = k.task AND s.ts >= k.start
        ORDER BY s.ts, s.seq
        """,
        [k[0] for k in keys],
        [k[1] for k in keys],
        [starts[k] for k in keys],
    )
    out: dict[tuple[str, str], list[dict]] = {}
    for r in recs:
        out.setdefault((r["plan"], r["task"]), []).append(dict(r))
    return out


def _plan(rec) -> dict[str, Any]:
    d = dict(rec)
    d["tasks"] = _json(d["tasks"])
    return d


async def plan_rows() -> list[dict[str, Any]]:
    rows = await get_pool().fetch(
        "SELECT plan, repo, base, host, tasks, updated_at FROM dispatch_plans "
        "ORDER BY updated_at DESC"
    )
    return [_plan(r) for r in rows]


async def plan_row(plan: str) -> dict[str, Any] | None:
    rec = await get_pool().fetchrow(
        "SELECT plan, repo, base, host, tasks, updated_at FROM dispatch_plans WHERE plan = $1",
        plan,
    )
    return _plan(rec) if rec else None


async def rows_for(plan: str | None = None) -> list[dict[str, Any]]:
    rows = await get_pool().fetch(
        """
        SELECT plan, task, ts, state, line, ref, cost_usd, session, meta, received_at
        FROM dispatch_rows
        WHERE $1::text IS NULL OR plan = $1
        ORDER BY ts, id
        """,
        plan,
    )
    return [{**dict(r), "meta": _json(r["meta"])} for r in rows]


def _latest(rows: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    return {(r["plan"], r["task"]): r for r in rows}


def _summary(plan: dict[str, Any], latest: dict[tuple[str, str], dict[str, Any]]) -> dict:
    states: dict[str, int] = {}
    for t in plan["tasks"]:
        row = latest.get((plan["plan"], t["id"]))
        state = row["state"] if row else "planned"
        states[state] = states.get(state, 0) + 1
    return {
        "plan": plan["plan"],
        "repo": plan["repo"],
        "base": plan["base"],
        "host": plan["host"],
        "updated_at": plan["updated_at"],
        "tasks_total": len(plan["tasks"]),
        "states": states,
    }


async def list_plans() -> list[dict[str, Any]]:
    latest = _latest(await rows_for())
    return [_summary(p, latest) for p in await plan_rows()]


async def get_plan(plan: str) -> dict[str, Any] | None:
    found = await plan_row(plan)
    if not found:
        return None
    history: dict[str, list[dict[str, Any]]] = {}
    for r in await rows_for(plan):
        history.setdefault(r["task"], []).append(r)
    starts = {}
    for t in found["tasks"]:
        start = run_start(history.get(t["id"], []))
        if start is not None:
            starts[(plan, t["id"])] = start
    steps = await steps_since(starts)
    tasks = []
    for t in found["tasks"]:
        run = steps.get((plan, t["id"]), [])
        tasks.append(
            {
                **t,
                "latest": (history.get(t["id"]) or [None])[-1],
                "rows": history.get(t["id"], []),
                "steps": [
                    {k: v for k, v in s.items() if k not in ("plan", "task")}
                    for s in run[-PLAN_STEPS_MAX:]
                ],
                "tools": sum(1 for s in run if s["kind"] == "tool"),
            }
        )
    return {**_summary(found, _latest([r for rs in history.values() for r in rs])), "tasks": tasks}


def _title(task: dict[str, Any]) -> str:
    goal = (task.get("goal") or "")[:60]
    return f"{task['id']}: {goal}" if goal else task["id"]


async def command_parts(now: datetime) -> tuple[list[dict], list[dict]]:
    plans = {p["plan"]: p for p in await plan_rows()}
    working, needs = [], []
    all_rows = await rows_for()
    history: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for r in all_rows:
        history.setdefault((r["plan"], r["task"]), []).append(r)
    latest = _latest(all_rows)
    starts = {}
    for key, row in latest.items():
        if row["state"] == "running":
            start = run_start(history[key])
            if start is not None:
                starts[key] = start
    steps = await steps_since(starts)
    for (plan, task), row in latest.items():
        meta = plans.get(plan, {})
        defn = next((t for t in meta.get("tasks", []) if t["id"] == task), {"id": task})
        seen = row.get("received_at")
        age = (now - seen).total_seconds() if seen else now.timestamp() - row["ts"]
        if row["state"] == "running":
            run = [s for s in steps.get((plan, task), []) if s["kind"] == "tool"]
            working.append(
                {
                    "kind": "dispatch",
                    "plan": plan,
                    "task": task,
                    "title": _title(defn),
                    "model": defn.get("model"),
                    "profile": defn.get("profile"),
                    "elapsed_s": int(age),
                    "line": row["line"],
                    "steps": [f"{s['tool']}: {s['text']}" for s in run[-2:][::-1]],
                    "tools": len(run),
                    "timeout_s": None,
                    "actions": [],
                }
            )
        elif row["state"] in NEEDS_STATES and age < NEEDS_WINDOW_S:
            needs.append(
                {
                    "id": f"dispatch:{plan}:{task}",
                    "kind": "dispatch",
                    "title": _title(defn),
                    "repo": service._short_repo(meta.get("repo")),
                    "next": _NEXT[row["state"]],
                    "question": row["line"] if row["state"] == "waiting" else None,
                    "actions": [],
                    "updated_at": datetime.fromtimestamp(row["ts"], tz=now.tzinfo),
                    "job_id": None,
                }
            )
    return working, needs
