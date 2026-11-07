"""Dispatch rows: idempotent insert, plan summary, and the Home summary entries."""

from datetime import UTC, datetime, timedelta

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
    assert await router.post_row(body) == {"ok": True}
    await router.post_row(body)
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


async def _summary_with(monkeypatch, rows):
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
    monkeypatch.setattr(dispatch, "rows_for", rows_for)
    monkeypatch.setattr(command.service, "cards", cards)
    monkeypatch.setattr(command.store, "working_jobs", working_jobs)
    monkeypatch.setattr(command.store, "finished_since", finished_since)
    return await command.summary()


async def test_home_summary_lists_running_and_waiting_dispatch_tasks(monkeypatch):
    rows = [_row("api", "running", 90, "editing"), _row("ui", "waiting", 600, "Which colour?")]
    result = await _summary_with(monkeypatch, rows)
    [running] = result["working"]
    assert (running["kind"], running["plan"], running["task"]) == ("dispatch", "ui-pass", "api")
    assert running["title"] == "api: " + PLAN["tasks"][0]["goal"][:60]
    assert running["line"] == "editing"
    assert running["steps"] == [] and running["tools"] == 0 and running["timeout_s"] is None
    assert 89 <= running["elapsed_s"] <= 100
    [needs] = result["needs"]
    assert needs["id"] == "dispatch:ui-pass:ui"
    assert needs["next"] == "Answer its question in the plan"
    assert needs["question"] == "Which colour?"
    assert needs["repo"] == "sazed"
    assert needs["job_id"] is None


async def test_home_summary_leaves_out_a_dispatch_need_older_than_a_day(monkeypatch):
    rows = [_row("ui", "failed", 25 * 3600), _row("docs", "conflict", 3600)]
    result = await _summary_with(monkeypatch, rows)
    assert [n["id"] for n in result["needs"]] == ["dispatch:ui-pass:docs"]
    assert result["needs"][0]["next"] == "Fix the plan and run it again"
