"""Paused jobs (plans/10 P1.7): a change job stopped by its timeout, tool cap or a worker
restart keeps its work on a draft PR and can be resumed; stopping never loses work."""

import uuid

import pytest

from app.automations import runner
from app.jobs import service
from app.routers import worker
from tests.test_activity_job_statuses import _latest_job_trigger, _status_map

JOB_ID = uuid.UUID("44444444-4444-4444-4444-444444444444")
PARENT_ID = uuid.UUID("55555555-5555-5555-5555-555555555555")


def _job(**overrides):
    job = {
        "id": JOB_ID,
        "kind": "code_task",
        "agent": "claude",
        "model": "claude-sonnet-5-5",
        "profile": "change",
        "title": "kelsier: big refactor",
        "status": "running",
        "params": {"repo": "tharpep/kelsier", "instruction": "big refactor"},
        "meta": {},
        "prompt": "p",
        "parent_id": None,
        "requested_by": "console",
        "agent_session_id": "s-1",
        "notify": False,
    }
    return job | overrides


@pytest.fixture
def finish_calls(monkeypatch):
    calls = {"finish": [], "pr": [], "job": _job()}

    async def fake_finish_job(job_id, status, result, error, session_id, meta):
        calls["finish"].append({"status": status, "result": result, "meta": meta})
        return _job(status=status, meta=meta, result=result)

    async def fake_get_job(job_id):
        return calls["job"]

    async def fake_open_pr(job, changes, draft=False):
        calls["pr"].append({"draft": draft, "meta": job["meta"], "files": sorted(changes)})
        return {
            "url": "https://github.com/x/y/pull/7",
            "number": 7,
            "branch": "sazed/x",
            "draft": draft,
        }

    async def fake_on_finished(job):
        return None

    monkeypatch.setattr(worker.store, "finish_job", fake_finish_job)
    monkeypatch.setattr(worker.store, "get_job", fake_get_job)
    monkeypatch.setattr(worker.service, "open_pull_request", fake_open_pr)
    monkeypatch.setattr(worker.service, "on_finished", fake_on_finished)
    return calls


def _stopped(stop_reason: str, status: str = "timed_out", **kw) -> worker.FinishRequest:
    fields = {
        "status": status,
        "error": "hit the job timeout",
        "agent_session_id": "s-1",
        "meta": {"stop_reason": stop_reason},
        "changes": {"a.py": "eA=="},
    }
    return worker.FinishRequest(**(fields | kw))


@pytest.mark.parametrize(
    ("stop_reason", "status"),
    [("timed_out", "timed_out"), ("tool_cap", "failed"), ("shutdown", "failed")],
)
async def test_a_stopped_change_job_pauses_with_a_draft_pr(finish_calls, stop_reason, status):
    await worker.finish(JOB_ID, _stopped(stop_reason, status))

    done = finish_calls["finish"][0]
    assert done["status"] == "paused"
    assert done["meta"]["pause_reason"] == stop_reason
    assert done["meta"]["pr_draft"] is True
    assert done["meta"]["pr_url"] == "https://github.com/x/y/pull/7"
    assert "**Draft pull request (opened):**" in done["result"]
    pr = finish_calls["pr"][0]
    assert (pr["draft"], pr["files"]) == (True, ["a.py"])
    assert pr["meta"]["pause_reason"] == stop_reason  # the draft's description says why


async def test_a_job_paused_before_changing_anything_opens_no_pr(finish_calls):
    await worker.finish(JOB_ID, _stopped("timed_out", changes={}))
    assert finish_calls["finish"][0]["status"] == "paused"
    assert finish_calls["pr"] == []


@pytest.mark.parametrize(
    ("job", "body"),
    [
        (_job(profile="review"), _stopped("timed_out")),  # not a change job
        (_job(), _stopped("cancelled", "failed")),  # Pryce stopped it: cancelled, not paused
        (_job(), _stopped("timed_out", agent_session_id=None)),  # nothing to resume
        (_job(status="cancelled"), _stopped("timed_out")),  # already settled on this side
    ],
)
async def test_other_stops_do_not_pause(finish_calls, job, body):
    finish_calls["job"] = job
    await worker.finish(JOB_ID, body)
    assert finish_calls["finish"][0]["status"] == body.status
    assert finish_calls["pr"] == []


async def test_a_question_still_comes_first(finish_calls):
    body = worker.FinishRequest(
        status="succeeded",
        result="NEEDS_INPUT: Which API?",
        agent_session_id="s-1",
        meta={"stop_reason": None},
        changes={"a.py": "eA=="},
    )
    await worker.finish(JOB_ID, body)
    assert finish_calls["finish"][0]["status"] == "needs_input"


# ---------------------------------------------------------------- resume


@pytest.fixture
def created(monkeypatch):
    out = []

    async def fake_create_job(**kwargs):
        out.append(kwargs)
        return kwargs

    async def no_cap(*args):
        return None

    monkeypatch.setattr(service.store, "create_job", fake_create_job)
    monkeypatch.setattr(service, "_check_cap", no_cap)
    return out


async def test_resuming_a_paused_job_queues_it_without_approval(monkeypatch, created):
    async def fake_get_job(job_id):
        return _job(status="paused", meta={"pause_reason": "tool_cap"})

    monkeypatch.setattr(service.store, "get_job", fake_get_job)
    job = await service.resume_job(JOB_ID, "console")

    assert job["status"] == "queued"
    assert job["parent_id"] == JOB_ID
    assert "stopped before finishing (tool cap)" in job["prompt"]


async def test_only_paused_jobs_resume(monkeypatch, created):
    async def fake_get_job(job_id):
        return _job(status="succeeded")

    monkeypatch.setattr(service.store, "get_job", fake_get_job)
    with pytest.raises(service.JobError):
        await service.resume_job(JOB_ID, "console")
    assert created == []


async def test_redirecting_a_paused_job_still_needs_approval(monkeypatch, created):
    async def fake_get_job(job_id):
        return _job(status="paused")

    monkeypatch.setattr(service.store, "get_job", fake_get_job)
    job = await service.create_follow_up(JOB_ID, "Do it differently.", "console")
    assert job["status"] == "needs_approval"


# ---------------------------------------------------------------- the PR call


class _Resp:
    is_success = True

    def json(self):
        return {"url": "u", "number": 7, "branch": "sazed/p", "draft": False}


@pytest.fixture
def pr_posts(monkeypatch):
    posts = []

    class _Client:
        async def post(self, url, json, headers, timeout):
            posts.append(json)
            return _Resp()

    async def fake_get_job(job_id):
        return _job(
            id=PARENT_ID,
            status="paused",
            meta={"branch": "sazed/p", "pr_draft": True, "pause_reason": "timed_out"},
        )

    monkeypatch.setattr(service, "get_client", lambda: _Client())
    monkeypatch.setattr(service.store, "get_job", fake_get_job)
    return posts


async def test_a_paused_job_opens_a_draft(pr_posts):
    job = _job(meta={"pause_reason": "timed_out"}, result="partial")
    await service.open_pull_request(job, {"a.py": "eA=="}, draft=True)

    sent = pr_posts[0]
    assert (sent["draft"], sent["ready"]) == (True, False)
    assert sent["branch"] == f"sazed/{JOB_ID}"
    assert "paused before it finished" in sent["body"]
    assert "partial" not in sent["body"]


async def test_finishing_a_resumed_draft_marks_it_ready(pr_posts):
    job = _job(parent_id=PARENT_ID, result="The full description.")
    await service.open_pull_request(job, {"a.py": "eA=="})

    sent = pr_posts[0]
    assert (sent["draft"], sent["ready"]) == (False, True)
    assert sent["branch"] == "sazed/p"
    assert sent["body"].startswith("The full description.")


# ---------------------------------------------------------------- telling Pryce


def test_a_paused_job_notifies_when_nobody_waits():
    assert service.should_notify(_job(status="paused", requested_by="console"))
    assert service.should_notify(_job(status="paused", requested_by="mcp"))
    assert not service.should_notify(_job(status="paused", requested_by="session:abc"))


def test_the_session_note_offers_a_resume():
    note = service.session_note(_job(status="paused", meta={"pause_reason": "timed_out"}))
    assert "paused" in note and "(timed out)" in note and str(JOB_ID) in note


def test_activity_shows_paused_as_pending():
    assert _status_map(_latest_job_trigger())["paused"] == "pending"


async def test_an_automation_waits_on_a_paused_job(monkeypatch):
    async def fake_create(*args, **kwargs):
        return _job(status="queued", params={"timeout_s": 60})

    async def fake_wait(job_id, timeout):
        return _job(status="paused", meta={"pause_reason": "tool_cap"})

    monkeypatch.setattr(service, "create_code_task", fake_create)
    monkeypatch.setattr(service, "wait_for", fake_wait)
    status, text, gist, link = await runner._run_job(
        {"kind": "code_task", "repo": "tharpep/kelsier", "input": "x", "mode": "change"},
        uuid.uuid4(),
    )
    assert status == "needs_approval"
    assert "paused (tool cap)" in text
