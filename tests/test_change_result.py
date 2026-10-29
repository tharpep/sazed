"""A change job's structured result (plans/10 P1.8): the reply's JSON block becomes the PR
description; a reply that doesn't fit keeps its prose; a question still pauses the job."""

import json
import uuid

import pytest

from app.jobs import change_result, service
from app.routers import worker

JOB_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")

FIELDS = {
    "summary": "Adds a retry to the sync call.",
    "files": ["app/sync.py: retry the fetch once on a 502"],
    "decisions": ["One retry, not backoff: the 502s are single blips"],
    "not_verified": ["Couldn't run the tests"],
    "needed_elsewhere": [],
    "manual_steps": [],
    "inert": "",
}


def _reply(fields: dict, before: str = "Done.") -> str:
    return f"{before}\n\n```json\n{json.dumps(fields, indent=1)}\n```\n"


def _change_job(**overrides):
    job = {
        "id": JOB_ID,
        "kind": "code_task",
        "agent": "claude",
        "model": "claude-sonnet-5-5",
        "profile": "change",
        "title": "kelsier: retry sync",
        "status": "running",
        "params": {"repo": "tharpep/kelsier", "instruction": "retry sync"},
        "meta": {},
        "prompt": "p",
        "parent_id": None,
        "agent_session_id": "s-1",
    }
    return job | overrides


@pytest.fixture
def finish_calls(monkeypatch):
    calls = {"finish": [], "pr": []}

    async def fake_finish_job(job_id, status, result, error, session_id, meta):
        calls["finish"].append({"status": status, "result": result, "meta": meta})
        return _change_job(status=status, meta=meta, result=result)

    async def fake_get_job(job_id):
        return _change_job()

    async def fake_pr_outcome(job, result, changes):
        calls["pr"].append(result)
        return result, {"pr_url": "https://github.com/x/y/pull/1"}

    async def fake_on_finished(job):
        return None

    monkeypatch.setattr(worker.store, "finish_job", fake_finish_job)
    monkeypatch.setattr(worker.store, "get_job", fake_get_job)
    monkeypatch.setattr(worker, "_pr_outcome", fake_pr_outcome)
    monkeypatch.setattr(worker.service, "on_finished", fake_on_finished)
    return calls


async def test_the_pr_description_is_rendered_from_the_fields(finish_calls):
    body = worker.FinishRequest(
        status="succeeded", result=_reply(FIELDS, "Some chatter."), changes={"a.py": "eA=="}
    )
    await worker.finish(JOB_ID, body)

    description = finish_calls["pr"][0]
    assert description.startswith("Adds a retry to the sync call.")
    assert "### Files\n- app/sync.py: retry the fetch once on a 502" in description
    assert "### Design decisions" in description
    assert "### Not verified" in description
    assert "Needed elsewhere" not in description  # empty sections are left out
    assert "Some chatter." not in description
    assert finish_calls["finish"][0]["result"] == description
    assert finish_calls["finish"][0]["meta"]["change_result"]["files"] == FIELDS["files"]


@pytest.mark.parametrize(
    "reply",
    [
        "Changed a.py to retry once.",  # no block at all
        _reply(FIELDS | {"files": "app/sync.py"}),  # a string where a list belongs
        _reply(FIELDS | {"summary": "  "}),
        "Done.\n\n```json\n{not json\n```",
    ],
)
async def test_a_reply_that_does_not_fit_keeps_its_prose(finish_calls, reply):
    body = worker.FinishRequest(status="succeeded", result=reply, changes={"a.py": "eA=="})
    await worker.finish(JOB_ID, body)

    assert finish_calls["pr"] == [reply]  # the PR still opens, with the reply as written
    assert finish_calls["finish"][0]["meta"]["result_format"] == "prose"
    assert "change_result" not in finish_calls["finish"][0]["meta"]


async def test_a_question_before_the_json_block_still_pauses(finish_calls):
    reply = _reply(FIELDS, "Half done.\nNEEDS_INPUT: Keep the old API too?")
    body = worker.FinishRequest(status="succeeded", result=reply, changes={"a.py": "eA=="})
    await worker.finish(JOB_ID, body)

    assert finish_calls["finish"][0]["status"] == "needs_input"
    assert finish_calls["finish"][0]["meta"]["question"] == "Keep the old API too?"
    assert finish_calls["pr"] == []


def test_the_last_block_wins():
    reply = _reply(FIELDS | {"summary": "first"}) + _reply(FIELDS | {"summary": "second"})
    assert change_result.parse(reply).summary == "second"


def test_inert_gets_its_own_section():
    fields = change_result.ChangeResult(
        summary="s", needed_elsewhere=["gateway route"], inert="Off until GATEWAY_X is set."
    )
    assert change_result.render(fields).endswith("### Until then\nOff until GATEWAY_X is set.")


async def test_the_change_prompt_asks_for_the_block_and_ends_with_the_ask_rule(monkeypatch):
    created = []

    async def fake_create_job(**kwargs):
        created.append(kwargs)
        return kwargs

    async def no_cap(*args):
        return None

    async def fake_repo_info(repo):
        return {"full_name": repo, "private": False}

    monkeypatch.setattr(service.settings, "worker_key", "test-key")
    monkeypatch.setattr(service.store, "create_job", fake_create_job)
    monkeypatch.setattr(service, "_check_cap", no_cap)
    monkeypatch.setattr(service, "_repo_info", fake_repo_info)

    await service.create_code_task("tharpep/kelsier", "retry sync", "change", "console")
    prompt = created[0]["prompt"]

    assert '{"summary":' in prompt  # the format rule's braces survived .format()
    for key in FIELDS:
        assert f'"{key}"' in prompt
    assert prompt.endswith(service._ASK_RULE)


async def test_a_change_follow_up_asks_for_the_block_again(monkeypatch):
    created = []

    async def fake_get_job(job_id):
        return _change_job(status="succeeded")

    async def fake_create_job(**kwargs):
        created.append(kwargs)
        return kwargs

    async def no_cap(*args):
        return None

    monkeypatch.setattr(service.store, "get_job", fake_get_job)
    monkeypatch.setattr(service.store, "create_job", fake_create_job)
    monkeypatch.setattr(service, "_check_cap", no_cap)

    job = await service.create_follow_up(JOB_ID, "Also log the retry.", requested_by="console")
    assert job["prompt"] == "Also log the retry." + change_result.FOLLOW_UP_RULE
