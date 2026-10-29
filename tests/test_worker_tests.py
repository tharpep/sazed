"""The worker runs the repo's tests after a change job's build (sazed_worker/tests.py, plans/10
P1.11). When new failures remain after its fix round, the PR opens as a draft that lists them."""

import uuid

import pytest

from app.jobs import service
from app.routers import worker

JOB_ID = uuid.UUID("88888888-8888-8888-8888-888888888888")


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


@pytest.fixture
def finish_calls(monkeypatch):
    calls = {"finish": [], "pr": []}

    async def fake_finish_job(job_id, status, result, error, session_id, meta):
        calls["finish"].append({"status": status, "result": result, "meta": meta})
        return _job(status=status, meta=meta, result=result)

    async def fake_get_job(job_id):
        return _job()

    async def fake_open_pr(job, changes, draft=False):
        calls["pr"].append({"draft": draft, "result": job["result"]})
        return {"url": "https://github.com/x/y/pull/9", "number": 9, "branch": "b", "draft": draft}

    async def fake_on_finished(job):
        return None

    monkeypatch.setattr(worker.store, "finish_job", fake_finish_job)
    monkeypatch.setattr(worker.store, "get_job", fake_get_job)
    monkeypatch.setattr(worker.service, "open_pull_request", fake_open_pr)
    monkeypatch.setattr(worker.service, "on_finished", fake_on_finished)
    return calls


def _done(tests_meta: dict) -> worker.FinishRequest:
    return worker.FinishRequest(
        status="succeeded",
        result="Done.",
        agent_session_id="s-1",
        changes={"a.py": "eA=="},
        meta={"tests": tests_meta},
    )


async def test_failing_tests_open_a_draft_that_lists_them(finish_calls):
    left = ["tests: tests/test_a.py::test_add fails after your change: assert 0 == 4"]
    await worker.finish(JOB_ID, _done({"runner": "pytest", "left": left}))

    pr = finish_calls["pr"][0]
    assert pr["draft"] is True
    assert "### Tests still failing" in pr["result"]
    assert "- tests/test_a.py::test_add fails after your change" in pr["result"]
    assert finish_calls["finish"][0]["status"] == "succeeded"
    assert finish_calls["finish"][0]["meta"]["pr_draft"] is True


@pytest.mark.parametrize(
    "tests_meta",
    [
        {"runner": "pytest", "left": []},  # passed, or fixed in the fix round
        {"runner": None, "left": []},  # the repo has no tests
        {"runner": "go", "skipped": "Go modules: go mod download failed", "left": []},
    ],
)
async def test_no_remaining_failures_opens_a_normal_pr(finish_calls, tests_meta):
    await worker.finish(JOB_ID, _done(tests_meta))
    assert finish_calls["pr"][0]["draft"] is False
    assert "Tests still failing" not in finish_calls["pr"][0]["result"]


async def test_a_test_draft_keeps_the_description_not_the_pause_note(monkeypatch):
    posts = []

    class _Resp:
        is_success = True

        def json(self):
            return {"url": "u", "number": 9, "branch": "b", "draft": True}

    class _Client:
        async def post(self, url, json, headers, timeout):
            posts.append(json)
            return _Resp()

    monkeypatch.setattr(service, "get_client", lambda: _Client())
    await service.open_pull_request(_job(result="The description."), {"a.py": "eA=="}, draft=True)

    assert posts[0]["draft"] is True
    assert posts[0]["body"].startswith("The description.")
    assert "paused" not in posts[0]["body"]
