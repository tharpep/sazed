"""Dispatch plans and their board rows, posted by sazed-worker's dispatch."""

import json
from datetime import datetime
from typing import Any

from app.board import service
from app.db import get_pool

NEEDS_STATES = ("waiting", "failed", "conflict", "blocked", "timed_out")
NEEDS_WINDOW_S = 24 * 3600
STEP_TEXT_MAX = 300
PLAN_STEPS_MAX = 80
_NEXT = {
    "waiting": "Answer its question in the plan",
    "failed": "Fix the plan and run it again",
    "conflict": "Fix the plan and run it again",
    "blocked": "Fix the plan and run it again",
    "timed_out": "Fix the plan and run it again",
}


def _json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


async def upsert_plan(
    plan: str,
    repo: str,
    base: str,
    host: str,
    tasks: list[dict],
    project: str | None = None,
    board_plan: str | None = None,
) -> None:
    await get_pool().execute(
        """
        INSERT INTO dispatch_plans (plan, repo, base, host, tasks, project, board_plan)
        VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7)
        ON CONFLICT (plan) DO UPDATE
        SET repo = $2, base = $3, host = $4, tasks = $5::jsonb, project = $6, board_plan = $7,
            updated_at = NOW()
        """,
        plan,
        repo,
        base,
        host,
        json.dumps(tasks),
        project,
        board_plan,
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


async def heartbeat(plan: str, task: str) -> None:
    await get_pool().execute(
        """
        INSERT INTO dispatch_heartbeats (plan, task) VALUES ($1, $2)
        ON CONFLICT (plan, task) DO UPDATE SET beat_at = NOW()
        """,
        plan,
        task,
    )


async def reap_silent(grace_s: int) -> list[dict[str, Any]]:
    """Fail each task whose latest row is running and that has sent neither a row nor a heartbeat
    for grace_s, with a line saying since when. The failed row's ts is the last heartbeat's time,
    or the running row's ts plus 1 ms when it never beat or its beat predates that row, so the
    console shows how long it ran and the row still sorts after the running one. Every caller
    reads the same pair (a newer beat would make the task not silent), so a second reaper's
    insert hits the unique key and does nothing."""
    rows = await get_pool().fetch(
        """
        WITH latest AS (
            SELECT DISTINCT ON (plan, task) plan, task, ts, state, received_at
            FROM dispatch_rows
            ORDER BY plan, task, ts DESC, id DESC
        ), silent AS (
            SELECT l.plan, l.task,
                   GREATEST(l.ts + 0.001, extract(epoch FROM h.beat_at)::float8) AS reaped_ts,
                   GREATEST(l.received_at, h.beat_at) AS last_seen
            FROM latest l
            LEFT JOIN dispatch_heartbeats h ON h.plan = l.plan AND h.task = l.task
            WHERE l.state = 'running'
              AND GREATEST(l.received_at, h.beat_at) < NOW() - make_interval(secs => $1::int)
        )
        INSERT INTO dispatch_rows (plan, task, ts, state, line, meta)
        SELECT plan, task, reaped_ts, 'failed',
               'no heartbeat since '
                   || to_char(last_seen AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS') || ' UTC',
               jsonb_build_object('stop_reason', 'no_heartbeat', 'last_heartbeat', last_seen)
        FROM silent
        ON CONFLICT (plan, task, ts, state) DO NOTHING
        RETURNING plan, task, line
        """,
        grace_s,
    )
    return [dict(r) for r in rows]


async def heartbeats(keys: list[tuple[str, str]]) -> dict[tuple[str, str], datetime]:
    if not keys:
        return {}
    recs = await get_pool().fetch(
        """
        SELECT h.plan, h.task, h.beat_at
        FROM dispatch_heartbeats h
        JOIN unnest($1::text[], $2::text[]) AS k(plan, task) ON h.plan = k.plan AND h.task = k.task
        """,
        [k[0] for k in keys],
        [k[1] for k in keys],
    )
    return {(r["plan"], r["task"]): r["beat_at"] for r in recs}


def this_run_beat(beat: datetime | None, start: float) -> datetime | None:
    """The heartbeat row outlives a run; a beat from before this run started isn't this run's."""
    if beat is None or beat.timestamp() < start:
        return None
    return beat


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


_PLAN_COLUMNS = (
    "plan, repo, base, host, tasks, project, board_plan, updated_at, "
    "pr_url, pr_number, pr_head_sha, exported_at"
)
PR_FIELDS = ("pr_url", "pr_number", "pr_head_sha", "exported_at")


async def plan_rows() -> list[dict[str, Any]]:
    rows = await get_pool().fetch(
        f"SELECT {_PLAN_COLUMNS} FROM dispatch_plans ORDER BY updated_at DESC"
    )
    return [_plan(r) for r in rows]


async def plan_row(plan: str) -> dict[str, Any] | None:
    rec = await get_pool().fetchrow(
        f"SELECT {_PLAN_COLUMNS} FROM dispatch_plans WHERE plan = $1", plan
    )
    return _plan(rec) if rec else None


async def record_export(plan: str, pr_url: str, pr_number: int, head_sha: str | None) -> None:
    await get_pool().execute(
        """
        UPDATE dispatch_plans
        SET pr_url = $2, pr_number = $3, pr_head_sha = $4, exported_at = NOW()
        WHERE plan = $1
        """,
        plan,
        pr_url,
        pr_number,
        head_sha,
    )


async def rows_for(plan: str | None = None) -> list[dict[str, Any]]:
    """Rows in order, each with its raw word as state and dispatch_state's word (migration 035)
    as run_state. A cleaned row is mapped with the latest row before it that is not cleaned."""
    rows = await get_pool().fetch(
        """
        SELECT r.plan, r.task, r.ts, r.state,
               dispatch_state(r.state, r.meta, b.state, b.meta) AS run_state,
               r.line, r.ref, r.cost_usd, r.session, r.meta, r.received_at
        FROM dispatch_rows r
        LEFT JOIN LATERAL (
            SELECT p.state, p.meta
            FROM dispatch_rows p
            WHERE r.state = 'cleaned' AND p.plan = r.plan AND p.task = r.task
              AND p.state <> 'cleaned' AND (p.ts, p.id) < (r.ts, r.id)
            ORDER BY p.ts DESC, p.id DESC
            LIMIT 1
        ) b ON TRUE
        WHERE $1::text IS NULL OR r.plan = $1
        ORDER BY r.ts, r.id
        """,
        plan,
    )
    return [{**dict(r), "meta": _json(r["meta"])} for r in rows]


def shaped_row(row: dict[str, Any] | None, v2: bool) -> dict[str, Any] | None:
    """The old shape keeps the raw word in state; the new one moves it to phase."""
    if row is None:
        return None
    out = {k: v for k, v in row.items() if k != "run_state"}
    return {**out, "state": row["run_state"], "phase": row["state"]} if v2 else out


def _latest(rows: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    return {(r["plan"], r["task"]): r for r in rows}


def _count(words: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for w in words:
        counts[w] = counts.get(w, 0) + 1
    return counts


def _summary(
    plan: dict[str, Any], latest: dict[tuple[str, str], dict[str, Any]], v2: bool = False
) -> dict:
    rows = [latest.get((plan["plan"], t["id"])) for t in plan["tasks"]]
    phases = _count([r["state"] if r else "planned" for r in rows])
    shaped: dict[str, Any] = {"states": phases}
    if v2:
        shaped = {
            "states": _count([r["run_state"] if r else "planned" for r in rows]),
            "phases": phases,
        } | {k: plan.get(k) for k in PR_FIELDS}
    return {
        "plan": plan["plan"],
        "repo": plan["repo"],
        "base": plan["base"],
        "host": plan["host"],
        "project": plan.get("project"),
        "board_plan": plan.get("board_plan"),
        "updated_at": plan["updated_at"],
        "tasks_total": len(plan["tasks"]),
        **shaped,
    }


async def list_plans(v2: bool = False) -> list[dict[str, Any]]:
    latest = _latest(await rows_for())
    return [_summary(p, latest, v2) for p in await plan_rows()]


async def runs_for_project(slug: str, v2: bool = False) -> list[dict[str, Any]]:
    latest = _latest(await rows_for())
    keys = ("plan", "updated_at", "tasks_total", "states", "phases")
    runs = []
    for p in await plan_rows():
        if p.get("project") == slug:
            s = _summary(p, latest, v2)
            runs.append({k: s[k] for k in keys if k in s})
    return runs


async def get_plan(plan: str, v2: bool = False) -> dict[str, Any] | None:
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
                "latest": shaped_row((history.get(t["id"]) or [None])[-1], v2),
                "rows": [shaped_row(r, v2) for r in history.get(t["id"], [])],
                "steps": [
                    {k: v for k, v in s.items() if k not in ("plan", "task")}
                    for s in run[-PLAN_STEPS_MAX:]
                ],
                "tools": sum(1 for s in run if s["kind"] == "tool"),
            }
        )
    latest = _latest([r for rs in history.values() for r in rs])
    return {**_summary(found, latest, v2), "tasks": tasks}


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
    beats = await heartbeats(list(starts))
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
                    "heartbeat_at": this_run_beat(beats.get((plan, task)), starts[(plan, task)]),
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
