"""Dispatch rows: idempotent insert, plan summary, and the Home summary entries."""

from datetime import UTC, datetime, timedelta

from fastapi import BackgroundTasks

from app.board import command, dispatch
from app.routers import dispatch as router

NOW = datetime.now(UTC)
PLAN = {
    "plan": "ui-pass",
    "repo": "tharpep/sazed",
    "base": "main",
    "host": "desktop",
    "updated_at": NOW,
    "tasks": [
        {"id": "api", "goal": "Add the endpoints " * 10, "model": "sonnet", "profile": "change"},
        {"id": "ui", "goal": "Draw the card", "model": "opus", "profile": "change"},
        {"id": "docs", "goal": "Write it up", "model": "haiku", "profile": "docs"},
    ],
}


def _row(task, state, age_s, line=""):
    return {
        "plan": "ui-pass",
        "task": task,
        "ts": (NOW - timedelta(seconds=age_s)).timestamp(),
        "state": state,
        "line": line,
        "ref": None,
        "cost_usd": None,
        "session": None,
        "meta": {},
    }


async def test_a_repeated_row_reaches_the_store_with_its_unique_key(monkeypatch):
    seen = []

    class Pool:
        async def execute(self, sql, *args):
            seen.append((sql, args))

    monkeypatch.setattr(dispatch, "get_pool", lambda: Pool())
    body = router.RowIn(plan="ui-pass", task="api", ts=1.5, state="running", line="go")
    assert await router.post_row(body, BackgroundTasks()) == {"ok": True}
    await router.post_row(body, BackgroundTasks())
    assert len(seen) == 2
    assert "ON CONFLICT (plan, task, ts, state) DO NOTHING" in seen[0][0]
    assert seen[0][1][:4] == ("ui-pass", "api", 1.5, "running")
    assert seen[0] == seen[1]


async def test_plan_summary_counts_latest_states_and_planned_tasks(monkeypatch):
    rows = [_row("api", "running", 300), _row("api", "done", 60), _row("ui", "waiting", 30)]

    async def plan_rows():
        return [PLAN]

    async def rows_for(plan=None):
        return rows

    monkeypatch.setattr(dispatch, "plan_rows", plan_rows)
    monkeypatch.setattr(dispatch, "rows_for", rows_for)
    [summary] = await dispatch.list_plans()
    assert summary["tasks_total"] == 3
    assert summary["states"] == {"done": 1, "waiting": 1, "planned": 1}


async def _summary_with(monkeypatch, rows, steps=(), beats=None):
    async def heartbeats(keys):
        return {k: v for k, v in (beats or {}).items() if k in keys}

    monkeypatch.setattr(dispatch, "heartbeats", heartbeats)

    async def plan_rows():
        return [PLAN]

    async def rows_for(plan=None):
        return rows

    async def cards():
        return []

    async def working_jobs():
        return []

    async def finished_since(hours):
        return []

    monkeypatch.setattr(dispatch, "plan_rows", plan_rows)

    async def steps_since(starts):
        return {
            k: [s for s in steps if (s["plan"], s["task"]) == k and s["ts"] >= start]
            for k, start in starts.items()
        }

    monkeypatch.setattr(dispatch, "rows_for", rows_for)
    monkeypatch.setattr(dispatch, "steps_since", steps_since)
    monkeypatch.setattr(command.store, "working_jobs", working_jobs)
    monkeypatch.setattr(command.store, "finished_since", finished_since)

    async def board():
        return command.sessions.BoardRead(await cards(), [], {})

    async def no_sessions(read, now):
        return {}

    monkeypatch.setattr(command.service, "read", board)

    monkeypatch.setattr(command.sessions, "command_part", no_sessions)
    return await command.summary()


async def test_home_summary_lists_running_and_waiting_dispatch_tasks(monkeypatch):
    rows = [_row("api", "running", 90, "editing"), _row("ui", "waiting", 600, "Which colour?")]
    result = await _summary_with(monkeypatch, rows)
    [running] = result["working"]
    assert (running["kind"], running["plan"], running["task"]) == ("dispatch", "ui-pass", "api")
    assert running["title"] == "api: " + PLAN["tasks"][0]["goal"][:60]
    assert running["line"] == "editing"
    assert running["steps"] == [] and running["tools"] == 0 and running["timeout_s"] is None
    since_import = (datetime.now(UTC) - NOW).total_seconds()
    assert 89 <= running["elapsed_s"] <= 90 + since_import
    [needs] = result["needs"]
    assert needs["id"] == "dispatch:ui-pass:ui"
    assert needs["next"] == "Answer its question in the plan"
    assert needs["question"] == "Which colour?"
    assert needs["repo"] == "sazed"
    assert needs["job_id"] is None


async def test_home_entries_never_carry_the_plans_job(monkeypatch):
    """Home keys its rows by job_id, so two tasks of one plan would collide."""
    monkeypatch.setitem(PLAN, "job_id", "job-1")
    rows = [_row("api", "running", 90), _row("ui", "waiting", 600), _row("docs", "failed", 60)]
    result = await _summary_with(monkeypatch, rows)
    assert all("job_id" not in w for w in result["working"])
    assert [n["job_id"] for n in result["needs"]] == [None, None]


async def test_a_running_dispatch_item_carries_its_last_heartbeat(monkeypatch):
    beat = NOW - timedelta(seconds=20)
    rows = [_row("api", "running", 90), _row("ui", "running", 90)]
    beats = {("ui-pass", "api"): beat}
    result = await _summary_with(monkeypatch, rows, beats=beats)
    by_task = {w["task"]: w["heartbeat_at"] for w in result["working"]}
    assert by_task == {"api": beat, "ui": None}


async def test_a_rerun_after_a_failed_row_drops_the_earlier_runs_beat(monkeypatch):
    rows = [_row("api", "running", 600), _row("api", "failed", 400), _row("api", "running", 60)]
    beats = {("ui-pass", "api"): NOW - timedelta(seconds=300)}
    result = await _summary_with(monkeypatch, rows, beats=beats)
    assert result["working"][0]["heartbeat_at"] is None


def test_a_heartbeat_from_before_this_run_started_is_not_this_runs():
    start = (NOW - timedelta(minutes=5)).timestamp()
    assert dispatch.this_run_beat(NOW - timedelta(minutes=9), start) is None
    assert dispatch.this_run_beat(NOW, start) == NOW
    assert dispatch.this_run_beat(None, start) is None


async def test_home_summary_leaves_out_a_dispatch_need_older_than_a_day(monkeypatch):
    rows = [_row("ui", "failed", 25 * 3600), _row("docs", "conflict", 3600)]
    result = await _summary_with(monkeypatch, rows)
    assert [n["id"] for n in result["needs"]] == ["dispatch:ui-pass:docs"]
    assert result["needs"][0]["next"] == "Fix the plan and run it again"


def _mapped(task, state, run_state, age_s, line=""):
    return {**_row(task, state, age_s, line), "run_state": run_state}


MAPPED_ROWS = [
    _mapped("api", "running", "running", 300),
    _mapped("api", "done", "succeeded", 60),
    _mapped("ui", "running", "running", 400),
    _mapped("ui", "failed", "timed_out", 30, "no heartbeat since 2026-11-19 10:00:00 UTC"),
]


def _fake_store(monkeypatch, rows):
    async def plan_rows():
        return [PLAN]

    async def plan_row(plan):
        return PLAN

    async def rows_for(plan=None):
        return rows

    async def steps_since(starts):
        return {}

    monkeypatch.setattr(dispatch, "plan_rows", plan_rows)
    monkeypatch.setattr(dispatch, "plan_row", plan_row)
    monkeypatch.setattr(dispatch, "rows_for", rows_for)
    monkeypatch.setattr(dispatch, "steps_since", steps_since)


async def test_without_the_header_rows_and_summaries_keep_the_raw_word(monkeypatch):
    _fake_store(monkeypatch, MAPPED_ROWS)
    [summary] = await router.list_plans(v2=False)
    assert summary["states"] == {"done": 1, "failed": 1, "planned": 1}
    assert "phases" not in summary
    found = await router.get_plan("ui-pass", v2=False)
    ui = found["tasks"][1]
    assert ui["latest"]["state"] == "failed"
    assert "phase" not in ui["latest"] and "run_state" not in ui["latest"]
    assert [r["state"] for r in ui["rows"]] == ["running", "failed"]


async def test_with_the_header_state_is_mapped_and_phase_is_raw(monkeypatch):
    _fake_store(monkeypatch, MAPPED_ROWS)
    [summary] = await router.list_plans(v2=True)
    assert summary["states"] == {"succeeded": 1, "timed_out": 1, "planned": 1}
    assert summary["phases"] == {"done": 1, "failed": 1, "planned": 1}
    found = await router.get_plan("ui-pass", v2=True)
    ui = found["tasks"][1]
    assert (ui["latest"]["state"], ui["latest"]["phase"]) == ("timed_out", "failed")
    assert ui["latest"]["line"].startswith("no heartbeat since ")
    assert [(r["state"], r["phase"]) for r in ui["rows"]] == [
        ("running", "running"),
        ("timed_out", "failed"),
    ]
    assert found["states"] == summary["states"]


def test_a_timed_out_row_from_the_worker_is_accepted():
    assert router.RowIn(plan="p", task="t", ts=1.0, state="timed_out").state == "timed_out"


async def test_a_reaped_task_still_needs_pryce_with_its_heartbeat_line(monkeypatch):
    rows = [_mapped("ui", "failed", "timed_out", 30, "no heartbeat since 2026-11-19 10:00:00 UTC")]
    result = await _summary_with(monkeypatch, rows)
    [needs] = result["needs"]
    assert needs["next"] == "Fix the plan and run it again"
    assert needs["title"].startswith("ui")


async def test_a_task_the_worker_timed_out_needs_pryce(monkeypatch):
    rows = [_mapped("api", "running", "running", 900), _mapped("api", "timed_out", "timed_out", 60)]
    result = await _summary_with(monkeypatch, rows)
    [needs] = result["needs"]
    assert needs["id"] == "dispatch:ui-pass:api"
    assert needs["next"] == "Fix the plan and run it again"
    assert result["working"] == []
