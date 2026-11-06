"""Bake-offs (My-AI plans/14): arms open no PR, queue last, have their own cap, and one arm is
promoted to a PR."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app import policy
from app.config import settings
from app.jobs import service, store
from app.policy.decide import _CAPPED_PREFIXES
from app.routers import worker

JOB_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")


def test_arms_queue_after_everything_else():
    assert store.claim_priority("bakeoff:ab12cd34") > store.claim_priority("automation-run:1")
    assert store.claim_priority("bakeoff:ab12cd34") > store.claim_priority("console")


def test_arms_have_their_own_cap_and_leave_the_others_alone():
    cap = settings.jobs_daily_cap_bakeoff
    assert policy.decide(policy.StartJob("claude", "bakeoff:x", used_today=cap - 1)).verdict == (
        "allow"
    )
    assert policy.decide(policy.StartJob("claude", "bakeoff:x", used_today=cap)).verdict == "block"
    assert not policy.counts_toward_cap("bakeoff:x")


async def test_count_recent_covers_every_capped_source(monkeypatch):
    sent = {}

    class Pool:
        async def fetchval(self, sql, *args):
            sent["args"] = args
            return 0

    monkeypatch.setattr(store, "get_pool", lambda: Pool())
    await store.count_recent("agy")
    assert sent["args"][1] == [f"{p}%" for p in _CAPPED_PREFIXES]
    assert "webhook%" in sent["args"][1]


@pytest.fixture
def created(monkeypatch):
    jobs = []
    state = {"used": 0}

    async def fake_create_code_task(repo, instruction, mode, **kw):
        jobs.append(kw)
        return {"id": uuid.uuid4()}

    async def fake_count():
        return state["used"]

    monkeypatch.setattr(service, "create_code_task", fake_create_code_task)
    monkeypatch.setattr(store, "count_bakeoff_recent", fake_count)
    return jobs, state


async def test_a_bakeoff_queues_one_result_only_arm_per_model(created):
    jobs, _ = created
    out = await service.create_bakeoff(
        "pryce/sazed", "Fix the typo", ["claude-sonnet-5-5", "claude-opus-5-5"], "mcp"
    )
    assert [a["arm"] for a in out["arms"]] == ["sonnet", "opus"]
    source = f"bakeoff:{out['bakeoff_id']}"
    assert all(j["requested_by"] == source and j["result_only"] for j in jobs)
    assert [j["variant"] for j in jobs] == [f"{source}:sonnet", f"{source}:opus"]


async def test_the_same_model_twice_gets_numbered_arms(created):
    out = await service.create_bakeoff(
        "pryce/sazed", "Fix it", ["claude-sonnet-5-5", "claude-sonnet-5-5"], "mcp"
    )
    assert [a["arm"] for a in out["arms"]] == ["sonnet", "sonnet-2"]


@pytest.mark.parametrize(
    "models",
    [["claude-sonnet-5-5"], ["claude-sonnet-5-5"] * 4, ["claude-sonnet-5-5", "gemini-3-pro"]],
)
async def test_bad_arms_queue_nothing(created, models):
    jobs, _ = created
    with pytest.raises(service.JobError):
        await service.create_bakeoff("pryce/sazed", "Fix it", models, "mcp")
    assert jobs == []


async def test_a_bakeoff_that_would_cross_the_cap_queues_nothing(created):
    jobs, state = created
    state["used"] = settings.jobs_daily_cap_bakeoff - 1
    with pytest.raises(service.JobError, match="cap"):
        await service.create_bakeoff(
            "pryce/sazed", "Fix it", ["claude-sonnet-5-5", "claude-opus-5-5"], "mcp"
        )
    assert jobs == []


async def test_a_finished_arm_keeps_its_files_and_opens_no_pr(monkeypatch):
    saved, prs = {}, []

    async def fake_get_job(job_id):
        return {"id": JOB_ID, "status": "running", "profile": "change", "kind": "code_task",
                "params": {"result_only": True}, "meta": {}}

    async def fake_save(job_id, changes):
        saved[job_id] = changes

    async def fake_pr(*a, **kw):
        prs.append(a)

    async def fake_finish_job(job_id, status, result, error, session_id, meta):
        saved["meta"], saved["result"] = meta, result
        return {"id": job_id, "status": status}

    async def fake_on_finished(job):
        pass

    monkeypatch.setattr(worker.store, "get_job", fake_get_job)
    monkeypatch.setattr(worker.store, "save_changes", fake_save)
    monkeypatch.setattr(worker.store, "finish_job", fake_finish_job)
    monkeypatch.setattr(worker.service, "open_pull_request", fake_pr)
    monkeypatch.setattr(worker.service, "on_finished", fake_on_finished)

    body = worker.FinishRequest(status="succeeded", result="Done.", changes={"a.py": "eA=="})
    await worker.finish(JOB_ID, body)

    assert saved[JOB_ID] == {"a.py": "eA=="}
    assert prs == []
    assert saved["meta"]["result_only"] is True
    assert "no PR opened" in saved["result"]


def _arm_row(arm, state="succeeded", created=0, promoted=False):
    t0 = datetime(2026, 11, 6, tzinfo=UTC) + timedelta(minutes=created)
    return {
        "job_id": uuid.uuid4(), "variant": f"bakeoff:ab12cd34:{arm}", "agent": "claude",
        "model": f"claude-{arm}-5-5", "state": state, "check_passed": state == "succeeded",
        "checks": {}, "usage": {"tokens": {"input_tokens": 1000, "output_tokens": 200}},
        "created_at": t0, "started_at": t0, "finished_at": t0 + timedelta(seconds=90),
        "params": {"repo": "pryce/sazed", "instruction": "Fix it"},
        "meta": {"tool_calls": 12, "promoted": promoted}, "changed_files": 2,
    }


async def test_the_report_shows_each_arms_latest_run(monkeypatch):
    rows = [_arm_row("sonnet", "paused", 0), _arm_row("opus", created=1),
            _arm_row("sonnet", created=2)]

    async def fake_arms(bakeoff_id):
        return rows

    monkeypatch.setattr(store, "bakeoff_arms", fake_arms)
    report = await service.bakeoff_report("ab12cd34")
    assert [(a["arm"], a["state"]) for a in report["arms"]] == [
        ("sonnet", "succeeded"), ("opus", "succeeded")]
    assert report["done"] and report["arms"][0]["seconds"] == 90
    assert report["arms"][0]["tool_calls"] == 12


async def test_promote_opens_one_pr_once(monkeypatch):
    rows = [_arm_row("sonnet"), _arm_row("opus", created=1)]
    opened, updated = [], {}

    async def fake_arms(bakeoff_id):
        return rows

    async def fake_get_job(job_id):
        return {"id": job_id, "status": "succeeded", "result": "Done.", "params": {}}

    async def fake_changes(job_id):
        return {"a.py": "eA=="}

    async def fake_pr(job, changes, draft=False):
        opened.append((job["id"], draft))
        return {"url": "https://github.com/x/y/pull/9", "number": 9, "branch": "sazed/x"}

    async def fake_update(job_id, result, meta):
        updated[job_id] = meta

    monkeypatch.setattr(store, "bakeoff_arms", fake_arms)
    monkeypatch.setattr(store, "get_job", fake_get_job)
    monkeypatch.setattr(store, "get_changes", fake_changes)
    monkeypatch.setattr(service, "open_pull_request", fake_pr)
    monkeypatch.setattr(store, "update_outcome", fake_update)

    out = await service.promote_bakeoff("ab12cd34", "opus")
    assert opened == [(rows[1]["job_id"], False)]
    assert out["pr_url"].endswith("/9") and updated[rows[1]["job_id"]]["promoted"]

    rows[1]["meta"]["promoted"] = True
    with pytest.raises(service.JobError, match="already"):
        await service.promote_bakeoff("ab12cd34", "sonnet")


async def test_a_failed_arm_cannot_be_promoted(monkeypatch):
    async def fake_arms(bakeoff_id):
        return [_arm_row("sonnet", "failed")]

    monkeypatch.setattr(store, "bakeoff_arms", fake_arms)
    with pytest.raises(service.JobError, match="failed"):
        await service.promote_bakeoff("ab12cd34", "sonnet")
