"""CI auto-fix (plans/10 P1.10): a closed or merged PR is left alone, a red one on an open PR gets
a fix-up, and a follow-up tells the gateway which PR it continues. From the first real runs
(2026-10-29), where fix-ups started after their PRs were closed and one reopened the work."""

import uuid

import pytest

from app.jobs import change_result, ci_watch, service

JOB_ID = uuid.UUID("66666666-6666-6666-6666-666666666666")
PARENT_ID = uuid.UUID("77777777-7777-7777-7777-777777777777")


def _job(**overrides):
    job = {
        "id": JOB_ID,
        "kind": "code_task",
        "agent": "claude",
        "model": "claude-sonnet-5-5",
        "profile": "change",
        "title": "sazed: add a thing",
        "status": "succeeded",
        "params": {"repo": "tharpep/sazed", "instruction": "add a thing"},
        "meta": {"pr_number": 7, "pr_url": "https://github.com/x/y/pull/7", "head_sha": "abc"},
        "prompt": "p",
        "parent_id": None,
        "requested_by": "console",
        "agent_session_id": "s-1",
    }
    return job | overrides


@pytest.fixture
def watch(monkeypatch):
    calls = {"pr": {"state": "open", "merged": False}, "ci": None, "outcome": [], "fixups": []}

    async def fake_pr(repo, number):
        return calls["pr"]

    async def fake_ci(repo, sha):
        calls["ci_asked"] = True
        return calls["ci"]

    async def fake_outcome(job_id, result, meta):
        calls["outcome"].append(meta)

    async def fake_follow_up(parent_id, instruction, requested_by, extra_params=None):
        calls["fixups"].append({"instruction": instruction, "requested_by": requested_by})
        return {"id": uuid.uuid4()}

    async def fake_notify(*args, **kwargs):
        calls["notified"] = True

    monkeypatch.setattr(ci_watch, "_pr", fake_pr)
    monkeypatch.setattr(ci_watch, "_ci", fake_ci)
    monkeypatch.setattr(ci_watch.store, "update_outcome", fake_outcome)
    monkeypatch.setattr(ci_watch.service, "create_follow_up", fake_follow_up)
    monkeypatch.setattr(ci_watch.service, "notify_simple", fake_notify)
    return calls


@pytest.mark.parametrize(("merged", "state"), [(False, "closed"), (True, "merged")])
async def test_a_closed_or_merged_pr_is_left_alone(watch, merged, state):
    watch["pr"] = {"state": "closed", "merged": merged}
    watch["ci"] = {"state": "failure", "failures": [], "runs": []}

    await ci_watch._check(_job())

    assert watch["outcome"] == [{"ci_state": state}]
    assert watch["fixups"] == []
    assert "ci_asked" not in watch and "notified" not in watch


async def test_an_unknown_pr_state_waits_for_the_next_tick(watch):
    watch["pr"] = None
    await ci_watch._check(_job())
    assert watch["outcome"] == [] and watch["fixups"] == [] and "ci_asked" not in watch


async def test_red_ci_on_an_open_pr_still_gets_a_fix_up(watch):
    watch["ci"] = {
        "state": "failure",
        "runs": [],
        "failures": [
            {"workflow": "CI", "job": "tests", "failed_steps": ["pytest"], "log_tail": "boom"}
        ],
    }

    await ci_watch._check(_job())

    assert len(watch["fixups"]) == 1
    assert watch["fixups"][0]["requested_by"] == f"ci-fix:{JOB_ID}"
    assert watch["outcome"][-1]["ci_state"] == "fixing"


def test_the_fix_up_wording_matches_the_worker_today():
    text = ci_watch._fix_instruction({"failures": []}, 1)
    assert "can't run anything" not in text  # the shell note says when tests can run
    assert "PR description" not in text  # the follow-up rule asks for the JSON block
    assert "attempt 1 of 2" in text


async def test_a_follow_up_names_the_pr_it_continues(monkeypatch):
    posts = []

    class _Resp:
        is_success = True

        def json(self):
            return {"url": "u", "number": 7, "branch": "sazed/p", "draft": False}

    class _Client:
        async def post(self, url, json, headers, timeout):
            posts.append(json)
            return _Resp()

    async def fake_get_job(job_id):
        return _job(id=PARENT_ID, meta={"branch": "sazed/p", "pr_number": 7})

    monkeypatch.setattr(service, "get_client", lambda: _Client())
    monkeypatch.setattr(service.store, "get_job", fake_get_job)

    await service.open_pull_request(_job(parent_id=PARENT_ID, result="Fixed."), {"a.py": "eA=="})
    assert posts[0]["pr_number"] == 7
    assert posts[0]["branch"] == "sazed/p"

    posts.clear()
    await service.open_pull_request(_job(result="New."), {"a.py": "eA=="})
    assert posts[0]["pr_number"] is None  # a new change opens a new PR


async def test_a_refused_follow_up_keeps_the_work_out_of_github(monkeypatch):
    class _Resp:
        is_success = False
        text = "conflict"

        def json(self):
            return {"detail": "PR #7 is closed or no longer on sazed/p; not reopening it"}

    class _Client:
        async def post(self, url, json, headers, timeout):
            return _Resp()

    async def fake_get_job(job_id):
        return _job(id=PARENT_ID, meta={"branch": "sazed/p", "pr_number": 7})

    monkeypatch.setattr(service, "get_client", lambda: _Client())
    monkeypatch.setattr(service.store, "get_job", fake_get_job)

    with pytest.raises(service.JobError, match="not reopening"):
        await service.open_pull_request(_job(parent_id=PARENT_ID), {"a.py": "eA=="})
    assert change_result.FOLLOW_UP_RULE  # the follow-up rule the fix-up relies on still exists
