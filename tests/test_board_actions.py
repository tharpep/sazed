"""Card actions and the Home summary: a card offers only what its job's state allows, and each
action reaches the job the way the console's buttons promise."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.board import actions, command
from app.jobs import service

JOB_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
NOW = datetime(2026, 11, 7, 18, 0, tzinfo=UTC)


def _job(status="running", **meta):
    return {
        "id": JOB_ID,
        "status": status,
        "meta": meta,
        "params": {"repo": "tharpep/sazed", "timeout_s": 1800},
        "title": "Board: actions on cards",
        "agent": "claude",
        "model": "sonnet",
        "profile": "change",
        "started_at": NOW - timedelta(minutes=6),
    }


@pytest.mark.parametrize(
    ("job", "offered"),
    [
        (_job("needs_approval"), ["approve", "decline"]),
        (_job("needs_input"), ["answer", "stop"]),
        (_job("running"), ["steer", "stop"]),
        (_job("queued"), ["stop"]),
        (_job("paused", pause_reason="timed_out"), ["resume", "stop"]),
        (_job("paused", pause_reason="rate_limit"), []),
        (_job("succeeded", pr_number=27, ci_state="success"), ["merge"]),
        (_job("succeeded", pr_number=27, ci_state="no_ci"), ["merge"]),
        (_job("succeeded", pr_number=27, ci_state="success", pr_draft=True), []),
        (_job("succeeded", pr_number=27, ci_state="failure"), []),
        (_job("succeeded", pr_number=27, ci_state="merged"), []),
        (_job("succeeded"), []),
        (_job("failed"), []),
        (None, []),
    ],
)
def test_a_card_offers_only_what_its_job_allows(job, offered):
    assert actions.offered(job) == offered


def test_text_actions_say_so():
    described = actions.describe(_job("needs_input"))
    assert described == [
        {"id": "answer", "label": "Answer", "takes_text": True},
        {"id": "stop", "label": "Stop", "takes_text": False},
    ]


@pytest.fixture
def calls(monkeypatch):
    seen: list[tuple] = []
    state = {"job": _job("needs_input")}

    async def get_job(job_id):
        return state["job"]

    async def follow_up(parent_id, instruction, requested_by, approved=False):
        seen.append(("follow_up", instruction, requested_by, approved))
        return {"id": "child"}

    async def cancel(job_id):
        seen.append(("cancel",))
        state["job"] = state["job"] | {"status": "cancelled", "agent_session_id": "s1"}
        return state["job"]

    async def record(job):
        return None

    async def update_outcome(job_id, result, patch):
        seen.append(("outcome", patch))

    monkeypatch.setattr(actions.jobs, "get_job", get_job)
    monkeypatch.setattr(actions.jobs, "cancel_job", cancel)
    monkeypatch.setattr(actions.jobs, "update_outcome", update_outcome)
    monkeypatch.setattr(actions.jobs_service, "create_follow_up", follow_up)
    monkeypatch.setattr(actions.learning, "record", record)
    return seen, state


async def test_an_action_the_card_does_not_offer_is_refused(calls):
    with pytest.raises(actions.ActionError, match="merge isn't available"):
        await actions.run(JOB_ID, "merge")
    assert calls[0] == []


async def test_an_answer_resumes_the_session_without_asking_again(calls):
    await actions.run(JOB_ID, "answer", "Follow the spec")
    assert calls[0] == [("follow_up", "Follow the spec", "console", True)]


async def test_an_empty_answer_is_refused(calls):
    with pytest.raises(actions.ActionError, match="needs a message"):
        await actions.run(JOB_ID, "answer", " ")


async def test_steer_stops_then_redirects_the_same_session(calls):
    seen, state = calls
    state["job"] = _job("running")
    await actions.run(JOB_ID, "steer", "Only touch CardRow.tsx")
    assert seen == [("cancel",), ("follow_up", "Only touch CardRow.tsx", "console", True)]


async def test_merge_goes_through_the_gateway_and_marks_the_pr_merged(calls, monkeypatch):
    seen, state = calls
    state["job"] = _job("succeeded", pr_number=27, ci_state="success")
    put_urls = []

    class Resp:
        status_code = 200

        def json(self):
            return {"merged": True}

    class Client:
        async def put(self, url, **kwargs):
            put_urls.append(url)
            return Resp()

    monkeypatch.setattr(actions, "get_client", lambda: Client())
    assert await actions.run(JOB_ID, "merge") == {"merged": True}
    assert put_urls[0].endswith("/github/repos/tharpep/sazed/pulls/27/merge")
    assert seen == [("outcome", {"ci_state": "merged"})]


def test_a_working_job_shows_its_newest_steps_first_and_its_clock():
    view = command.working_view(_job("running"), ["Read: a", "Edit: b", "Bash: pnpm build"], NOW)
    assert view["steps"] == ["Bash: pnpm build", "Edit: b"]
    assert (view["tools"], view["elapsed_s"], view["timeout_s"]) == (3, 360, 1800)
    assert view["repo"] == "sazed"
    assert [a["id"] for a in view["actions"]] == ["steer", "stop"]


def test_done_counts_failures_and_keeps_the_newest_few():
    rows = [
        {"id": JOB_ID, "status": s, "title": f"job {i}", "repo": "tharpep/kelsier",
         "finished_at": NOW}
        for i, s in enumerate(["succeeded", "failed", "succeeded", "timed_out", "succeeded",
                               "succeeded"])
    ]
    done = command.done_view(rows)
    assert (done["succeeded"], done["failed"]) == (4, 2)
    assert len(done["latest"]) == command.DONE_SHOWN
    assert done["latest"][0]["repo"] == "kelsier"


def test_an_agy_step_keeps_the_time_it_started():
    t0, t1 = NOW, NOW + timedelta(seconds=9)
    step = {"step_type": "tool", "step_index": 4,
            "tool_info": {"name": "read_url", "parameters": {"url": "https://x"}}}
    events = [{"at": t0, "event": {"step_update": step}},
              {"at": t1, "event": {"step_update": step}}]
    assert service.step_timeline(events) == [{"at": t0, "text": "read_url: https://x"}]
