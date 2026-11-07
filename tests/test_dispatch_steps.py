"""Dispatch steps: idempotent truncating insert, current-run filter, plan detail, Home summary."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.board import dispatch
from app.routers import dispatch as router
from tests.test_dispatch_rows import PLAN, _row, _summary_with


def _step(ts, seq, kind="tool", tool="Edit", text="x", error=False):
    return {
        "plan": "ui-pass",
        "task": "api",
        "ts": ts,
        "seq": seq,
        "kind": kind,
        "tool": tool,
        "text": text,
        "error": error,
        "received_at": datetime.now(UTC),
    }


def _stub_steps(monkeypatch, steps):
    async def steps_since(starts):
        return {
            k: [s for s in steps if s["ts"] >= start and (s["plan"], s["task"]) == k]
            for k, start in starts.items()
        }

    monkeypatch.setattr(dispatch, "steps_since", steps_since)


async def test_steps_insert_is_idempotent_and_truncates_text(monkeypatch):
    seen = []

    class Pool:
        async def executemany(self, sql, args):
            seen.append((sql, args))

    monkeypatch.setattr(dispatch, "get_pool", lambda: Pool())
    body = router.StepsIn(
        plan="ui-pass",
        task="api",
        steps=[router.StepIn(ts=1.5, seq=0, kind="tool", tool="Bash", text="y" * 500)],
    )
    assert await router.post_steps(body) == {"ok": True, "stored": 1}
    [(sql, args)] = seen
    assert "ON CONFLICT (plan, task, ts, seq) DO NOTHING" in sql
    assert args[0][:5] == ("ui-pass", "api", 1.5, 0, "tool")
    assert len(args[0][6]) == 300


def test_steps_batch_is_one_to_two_hundred_and_kinds_are_fixed():
    step = {"ts": 1.0, "seq": 0, "kind": "text"}
    with pytest.raises(ValidationError):
        router.StepsIn(plan="p", task="t", steps=[])
    with pytest.raises(ValidationError):
        router.StepsIn(plan="p", task="t", steps=[step] * 201)
    with pytest.raises(ValidationError):
        router.StepsIn(plan="p", task="t", steps=[{**step, "kind": "other"}])
    assert len(router.StepsIn(plan="p", task="t", steps=[step] * 200).steps) == 200


def test_run_start_follows_the_last_cleaned_row():
    rows = [
        _row("api", "running", 900),
        _row("api", "cleaned", 800),
        _row("api", "running", 700),
        _row("api", "done", 600),
    ]
    assert dispatch.run_start(rows) == rows[2]["ts"]
    assert dispatch.run_start(rows[2:]) == 0.0
    assert dispatch.run_start(rows[:2]) is None


async def test_the_current_run_drops_steps_from_before_the_last_cleaned_row(monkeypatch):
    rows = [_row("api", "running", 900), _row("api", "cleaned", 800), _row("api", "running", 700)]
    steps = [_step(rows[0]["ts"] + 1, 0, text="old"), _step(rows[2]["ts"] + 1, 0, text="new")]

    async def plan_row(plan):
        return PLAN

    async def rows_for(plan=None):
        return rows

    monkeypatch.setattr(dispatch, "plan_row", plan_row)
    monkeypatch.setattr(dispatch, "rows_for", rows_for)
    _stub_steps(monkeypatch, steps)
    detail = await dispatch.get_plan("ui-pass")
    api = next(t for t in detail["tasks"] if t["id"] == "api")
    assert [s["text"] for s in api["steps"]] == ["new"]


async def test_plan_detail_carries_steps_and_tool_count(monkeypatch):
    rows = [_row("api", "running", 900)]
    kinds = ["tool", "result"]
    steps = [_step(rows[0]["ts"] + i, 0, kind=kinds[i % 2]) for i in range(200)]

    async def plan_row(plan):
        return PLAN

    async def rows_for(plan=None):
        return rows

    monkeypatch.setattr(dispatch, "plan_row", plan_row)
    monkeypatch.setattr(dispatch, "rows_for", rows_for)
    _stub_steps(monkeypatch, steps)
    detail = await dispatch.get_plan("ui-pass")
    api = next(t for t in detail["tasks"] if t["id"] == "api")
    ui = next(t for t in detail["tasks"] if t["id"] == "ui")
    assert len(api["steps"]) == 80
    assert api["steps"][-1]["ts"] == steps[-1]["ts"]
    assert set(api["steps"][0]) == {"ts", "seq", "kind", "tool", "text", "error", "received_at"}
    assert api["tools"] == 100
    assert ui["steps"] == [] and ui["tools"] == 0


async def test_home_summary_fills_dispatch_steps_newest_first_and_tool_count(monkeypatch):
    rows = [_row("api", "running", 900, "editing")]
    base = rows[0]["ts"]
    steps = [
        _step(base + 1, 0, tool="Read", text="a.py"),
        _step(base + 2, 1, kind="result", tool=None, text="ok"),
        _step(base + 3, 2, tool="Edit", text="b.py"),
        _step(base + 4, 3, tool="Bash", text="pytest"),
    ]
    [running] = (await _summary_with(monkeypatch, rows, steps))["working"]
    assert running["steps"] == ["Bash: pytest", "Edit: b.py"]
    assert running["tools"] == 3
