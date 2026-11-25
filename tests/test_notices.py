import uuid

import httpx
import pytest

from app import notices
from app.automations import runner
from app.config import settings
from app.jobs import service

JOB = uuid.UUID(int=7)
CARD = uuid.UUID(int=8)
AUTO = uuid.UUID(int=9)


@pytest.fixture
def posted(monkeypatch):
    """Every POST to the gateway's /notify, captured; nothing reaches Pushover."""
    calls: list[dict] = []

    class FakeClient:
        async def post(self, url, json=None, headers=None, timeout=None):
            assert url == f"{settings.gateway_url}/notify"
            calls.append(json)
            return httpx.Response(200, json={"status": 1, "request": "r"})

    monkeypatch.setattr(notices, "get_client", lambda: FakeClient())
    return calls


@pytest.fixture
def card(monkeypatch):
    async def card_for_job(job_id):
        return CARD

    monkeypatch.setattr(service.store, "card_for_job", card_for_job)


async def test_a_notice_names_its_kind_and_ids_and_no_rule(posted):
    """Priority, expiry and link are the gateway's rule; Sazed never sends them."""
    assert await notices.notify_simple("job_done", "T", "B", {"job": "j"})
    assert posted == [{"kind": "job_done", "title": "T", "message": "B", "refs": {"job": "j"}}]


def _job(status, **meta):
    return {
        "id": JOB,
        "kind": "code_task",
        "status": status,
        "title": "Fix it",
        "meta": meta,
        "params": {"repo": "tharpep/sazed"},
        "result": "Done. It works.",
        "error": "boom",
    }


@pytest.mark.parametrize(
    ("job", "kind"),
    [
        (_job("needs_input", question="Which repo?"), "question"),
        (_job("paused", pause_reason="timeout"), "run_failed"),
        (_job("paused", pause_reason="rate_limit"), "routine"),
        (_job("succeeded", pr_url="u", pr_number=3, pr_draft=True), "run_failed"),
        (_job("succeeded", pr_url="u", pr_number=3), "job_done"),
        (_job("succeeded"), "job_done"),
        (_job("failed"), "run_failed"),
        (_job("timed_out"), "run_failed"),
    ],
)
async def test_each_job_outcome_sends_its_kind_with_job_and_card(posted, card, job, kind):
    await service._notify(job)
    (sent,) = posted
    assert sent["kind"] == kind
    assert sent["refs"] == {"job": str(JOB), "card": str(CARD)}


async def test_a_job_without_a_card_still_links_to_the_job(posted, monkeypatch):
    async def broken(job_id):
        raise RuntimeError("no pool")

    monkeypatch.setattr(service.store, "card_for_job", broken)
    await service._notify(_job("succeeded"))
    assert posted[0]["refs"] == {"job": str(JOB)}


async def test_green_ci_is_a_merge_call_on_the_card(posted, card, monkeypatch):
    from app.jobs import ci_watch

    async def pr(repo, n):
        return {"state": "open"}

    async def ci(repo, sha):
        return {"state": "success", "runs": []}

    async def settle(job, meta):
        return None

    monkeypatch.setattr(ci_watch, "pull_request", pr)
    monkeypatch.setattr(ci_watch, "_ci", ci)
    monkeypatch.setattr(ci_watch, "_settle", settle)
    await ci_watch._check(_job("succeeded", pr_number=3, pr_url="u", head_sha="abc"))
    (sent,) = posted
    assert sent["kind"] == "merge_call"
    assert sent["refs"] == {"job": str(JOB), "card": str(CARD)}


def _automation(outputs):
    return {
        "id": AUTO,
        "name": "Morning brief",
        "outputs": outputs,
        "action": {"type": "gateway_call", "method": "GET", "path": "/x"},
    }


@pytest.fixture
def run_env(monkeypatch):
    finished: list[str] = []

    async def finish_run(run_id, status, **kw):
        finished.append(status)

    async def call(action):
        return "success", "All quiet."

    monkeypatch.setattr(runner.store, "finish_run", finish_run)
    monkeypatch.setattr(runner, "_run_gateway_call", call)
    monkeypatch.setattr(settings, "automation_alerts", True)
    return finished


async def test_automation_output_is_routine_and_opens_the_automation(posted, run_env):
    await runner._execute(_automation({"pushover": {"title": "Brief"}}), uuid.uuid4())
    (sent,) = posted
    assert (sent["kind"], sent["title"]) == ("routine", "Brief")
    assert sent["refs"] == {"automation": str(AUTO)}


async def test_a_run_parked_on_approval_asks_permission(posted, run_env, monkeypatch):
    async def call(action):
        return "needs_approval", "Waiting."

    monkeypatch.setattr(runner, "_run_gateway_call", call)
    await runner._execute(_automation({}), uuid.uuid4())
    (sent,) = posted
    assert sent["kind"] == "permission" and sent["refs"] == {"automation": str(AUTO)}


async def test_a_failed_run_says_so_once(posted, run_env, monkeypatch):
    async def call(action):
        raise RuntimeError("gateway down")

    monkeypatch.setattr(runner, "_run_gateway_call", call)
    await runner._execute(_automation({"pushover": {}}), uuid.uuid4())
    (sent,) = posted
    assert sent["kind"] == "run_failed" and sent["refs"] == {"automation": str(AUTO)}


async def test_no_second_message_when_recording_fails_after_delivery(posted, monkeypatch):
    """The output already reached the phone; a DB error after it is not a second event."""
    calls = 0

    async def finish_run(run_id, status, **kw):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("db gone")

    async def call(action):
        return "success", "All quiet."

    monkeypatch.setattr(runner.store, "finish_run", finish_run)
    monkeypatch.setattr(runner, "_run_gateway_call", call)
    monkeypatch.setattr(settings, "automation_alerts", True)
    await runner._execute(_automation({"pushover": {"title": "Brief"}}), uuid.uuid4())
    assert [p["kind"] for p in posted] == ["routine"]
