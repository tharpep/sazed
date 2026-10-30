"""Pryce's plan limit (plans/10 P1.2): a Claude run the plan refuses isn't a failure. A change
job pauses until the reset and resumes on its own; queued Claude jobs wait; the console says why."""

import time
import uuid

import pytest

from app.jobs import service
from app.routers import jobs as jobs_router
from app.routers import worker

JOB_ID = uuid.UUID("99999999-9999-9999-9999-999999999999")
RESET = int(time.time()) + 2 * 3600 + 15 * 60


def _job(**overrides):
    job = {
        "id": JOB_ID,
        "kind": "code_task",
        "agent": "claude",
        "model": "claude-sonnet-5-5",
        "profile": "change",
        "title": "sazed: add a thing",
        "status": "running",
        "params": {"repo": "tharpep/sazed", "instruction": "add a thing"},
        "meta": {},
        "prompt": "p",
        "parent_id": None,
        "requested_by": "console",
        "agent_session_id": "s-1",
    }
    return job | overrides


def _limited(**kw) -> worker.FinishRequest:
    fields = {
        "status": "failed",
        "error": "hit Pryce's five hour plan limit",
        "agent_session_id": "s-1",
        "meta": {
            "stop_reason": "rate_limit",
            "rate_limited": {"window": "five_hour", "resets_at": RESET},
        },
        "changes": {"a.py": "eA=="},
    }
    return worker.FinishRequest(**(fields | kw))


@pytest.fixture
def finish_calls(monkeypatch):
    calls = {"finish": [], "pr": [], "job": _job()}

    async def fake_finish_job(job_id, status, result, error, session_id, meta):
        calls["finish"].append({"status": status, "meta": meta, "error": error})
        return _job(status=status, meta=meta)

    async def fake_get_job(job_id):
        return calls["job"]

    async def fake_open_pr(job, changes, draft=False):
        calls["pr"].append(draft)
        return {"url": "u", "number": 3, "branch": "b", "draft": draft}

    async def fake_on_finished(job):
        return None

    monkeypatch.setattr(worker.store, "finish_job", fake_finish_job)
    monkeypatch.setattr(worker.store, "get_job", fake_get_job)
    monkeypatch.setattr(worker.service, "open_pull_request", fake_open_pr)
    monkeypatch.setattr(worker.service, "on_finished", fake_on_finished)
    return calls


async def test_a_change_job_at_the_limit_pauses_until_the_reset(finish_calls):
    await worker.finish(JOB_ID, _limited())
    done = finish_calls["finish"][0]
    assert done["status"] == "paused"
    assert done["meta"]["pause_reason"] == "rate_limit"
    assert done["meta"]["resume_after"] == RESET
    assert finish_calls["pr"] == [True]  # its work so far is saved as a draft


async def test_a_review_job_at_the_limit_fails_with_the_reset_in_its_error(finish_calls):
    finish_calls["job"] = _job(profile="review")
    error = "hit Pryce's five hour plan limit; it resets at 2026-10-30 01:00 UTC"
    await worker.finish(JOB_ID, _limited(error=error, changes=None))
    done = finish_calls["finish"][0]
    assert done["status"] == "failed" and done["error"] == error
    assert done["meta"]["rate_limited"]["resets_at"] == RESET  # the queue hold reads this


async def test_due_pauses_resume_once_with_their_own_requester(monkeypatch):
    due = [
        _job(status="paused", requested_by="console"),
        _job(id=uuid.uuid4(), status="paused", requested_by="ci-fix:x"),
        _job(id=uuid.uuid4(), status="paused", requested_by="automation-run:1"),
    ]
    resumed = []

    async def fake_due():
        return due

    async def fake_resume(job_id, requested_by):
        if requested_by.startswith("automation"):
            raise service.JobError("daily cap reached")
        resumed.append(requested_by)
        return {}

    monkeypatch.setattr(service.store, "due_rate_limit_resumes", fake_due)
    monkeypatch.setattr(service, "resume_job", fake_resume)

    assert await service.resume_rate_limited() == 2
    assert resumed == ["console", "ci-fix:x"]  # the capped one waits for the next tick


async def test_the_pause_notice_says_when_it_resumes(monkeypatch):
    sent = []

    async def fake_notify(title, body, url=None, url_title=None):
        sent.append((title, body))

    monkeypatch.setattr(service, "notify_simple", fake_notify)
    job = _job(
        status="paused",
        meta={"pause_reason": "rate_limit", "resume_after": RESET, "pr_url": "u", "pr_number": 3},
    )
    await service._notify(job)
    title, body = sent[0]
    assert title.endswith("paused (plan limit)")
    assert "resumes on its own in about 2h 1" in body
    assert "other Claude jobs wait until then" in body


@pytest.mark.parametrize(
    ("seconds", "text"),
    [
        (45 * 60, "in about 45m"),
        (3 * 3600 + 600, "in about 3h 10m"),
        (2 * 86400 + 3600, "in about 2d 1h"),
    ],
)
def test_until(seconds, text):
    assert service._until(time.time() + seconds + 30) == text


async def test_a_queued_claude_job_says_it_is_held(monkeypatch):
    async def fake_get_job(job_id):
        return _job(status="queued")

    async def none(*args):
        return []

    async def fake_until():
        return float(RESET)

    monkeypatch.setattr(jobs_router.store, "get_job", fake_get_job)
    monkeypatch.setattr(jobs_router.store, "get_recent_events", none)
    monkeypatch.setattr(jobs_router.store, "plan_limit_until", fake_until)
    job = await jobs_router.get_job(JOB_ID, events=False)
    assert job["held_until"] == float(RESET)


async def test_a_running_job_is_not_held(monkeypatch):
    async def fake_get_job(job_id):
        return _job(status="running")

    async def none(*args):
        return []

    monkeypatch.setattr(jobs_router.store, "get_job", fake_get_job)
    monkeypatch.setattr(jobs_router.store, "get_recent_events", none)
    job = await jobs_router.get_job(JOB_ID, events=False)
    assert job["held_until"] is None
