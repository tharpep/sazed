"""Dispatch plans and their board rows, posted by sazed-worker's dispatch."""

import json
from datetime import datetime
from typing import Any

from app.board import service
from app.db import get_pool

NEEDS_STATES = ("waiting", "failed", "conflict", "blocked")
NEEDS_WINDOW_S = 24 * 3600
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
        SELECT plan, task, ts, state, line, ref, cost_usd, session, meta
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
    tasks = [
        {**t, "latest": (history.get(t["id"]) or [None])[-1], "rows": history.get(t["id"], [])}
        for t in found["tasks"]
    ]
    return {**_summary(found, _latest([r for rs in history.values() for r in rs])), "tasks": tasks}


def _title(task: dict[str, Any]) -> str:
    goal = (task.get("goal") or "")[:60]
    return f"{task['id']}: {goal}" if goal else task["id"]


async def command_parts(now: datetime) -> tuple[list[dict], list[dict]]:
    plans = {p["plan"]: p for p in await plan_rows()}
    working, needs = [], []
    for (plan, task), row in _latest(await rows_for()).items():
        meta = plans.get(plan, {})
        defn = next((t for t in meta.get("tasks", []) if t["id"] == task), {"id": task})
        age = now.timestamp() - row["ts"]
        if row["state"] == "running":
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
                    "steps": [],
                    "tools": 0,
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
