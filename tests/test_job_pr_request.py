"""The gateway request a worker job's PR makes, pinned field for field (One stack seam 6, S6-6):
exporting a plan shares this call, and a job's PR must not change because of it."""

import uuid

import pytest

from app.jobs import service

JOB_ID = uuid.UUID("66666666-6666-6666-6666-666666666666")
PARENT_ID = uuid.UUID("77777777-7777-7777-7777-777777777777")
CHANGES = {"a.py": "eA==", "old.py": None}


def _job(**overrides):
    job = {
        "id": JOB_ID,
        "model": "claude-sonnet-5-5",
        "title": "kelsier: tidy the loader",
        "params": {"repo": "tharpep/kelsier", "instruction": "tidy the loader"},
        "meta": {},
        "prompt": "Follow-up: fix the failing test",
        "parent_id": None,
        "result": "Tidied it.",
    }
    return job | overrides


@pytest.fixture
def sent(monkeypatch):
    calls = []

    class _Resp:
        is_success = True

        def json(self):
            return {"url": "u", "number": 7, "branch": "b", "draft": False}

    class _Client:
        async def post(self, url, **kwargs):
            calls.append({"url": url, **kwargs})
            return _Resp()

    async def fake_get_job(job_id):
        assert job_id == PARENT_ID
        return {"meta": {"branch": "sazed/parent", "pr_number": 41, "pr_draft": True}}

    monkeypatch.setattr(service.settings, "gateway_url", "http://gw")
    monkeypatch.setattr(service.settings, "gateway_api_key", "gw-key")
    monkeypatch.setattr(service, "get_client", lambda: _Client())
    monkeypatch.setattr(service.store, "get_job", fake_get_job)
    return calls


def _call(json):
    return {
        "url": "http://gw/github/app/pull-request",
        "json": json,
        "headers": {"X-API-Key": "gw-key"},
        "timeout": 120.0,
    }


def _body(text):
    return (
        f"{text}\n\n---\n"
        f"Opened by Sazed's worker (Claude Code, `claude-sonnet-5-5`) for job `{JOB_ID}`.\n"
        "Task: tidy the loader"
    )


async def test_a_new_change_opens_a_ready_pr_on_its_own_branch(sent):
    await service.open_pull_request(_job(), CHANGES)
    assert sent == [
        _call(
            {
                "repo": "tharpep/kelsier",
                "branch": f"sazed/{JOB_ID}",
                "title": "tidy the loader",
                "body": _body("Tidied it."),
                "commit_message": f"tidy the loader\n\nSazed job {JOB_ID}",
                "files": CHANGES,
                "draft": False,
                "ready": False,
                "pr_number": None,
            }
        )
    ]


async def test_a_paused_change_opens_a_draft(sent):
    job = _job(meta={"pause_reason": "tool_cap"})
    await service.open_pull_request(job, CHANGES, draft=True)
    text = (
        "**Draft: paused before it finished** (tool cap). This is the work so far; resuming "
        "the job continues it here and marks the PR ready."
    )
    assert sent == [
        _call(
            {
                "repo": "tharpep/kelsier",
                "branch": f"sazed/{JOB_ID}",
                "title": "tidy the loader",
                "body": _body(text),
                "commit_message": f"tidy the loader\n\nSazed job {JOB_ID}",
                "files": CHANGES,
                "draft": True,
                "ready": False,
                "pr_number": None,
            }
        )
    ]


async def test_a_follow_up_continues_its_parents_draft_and_marks_it_ready(sent):
    await service.open_pull_request(_job(parent_id=PARENT_ID), CHANGES)
    assert sent == [
        _call(
            {
                "repo": "tharpep/kelsier",
                "branch": "sazed/parent",
                "title": "tidy the loader",
                "body": _body("Tidied it."),
                "commit_message": f"Follow-up: fix the failing test\n\nSazed job {JOB_ID}",
                "files": CHANGES,
                "draft": False,
                "ready": True,
                "pr_number": 41,
            }
        )
    ]


async def test_a_paused_follow_up_stays_a_draft_on_its_parents_branch(sent):
    await service.open_pull_request(_job(parent_id=PARENT_ID), CHANGES, draft=True)
    assert sent[0]["json"]["branch"] == "sazed/parent"
    assert (sent[0]["json"]["draft"], sent[0]["json"]["ready"]) == (True, False)
    assert sent[0]["json"]["pr_number"] == 41
    assert set(sent[0]["json"]) == {
        "repo",
        "branch",
        "title",
        "body",
        "commit_message",
        "files",
        "draft",
        "ready",
        "pr_number",
    }
