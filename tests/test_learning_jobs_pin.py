"""Pins what worker jobs get from learning before dispatch failures joined the ledger (seam 5,
S5-7): their classes, the class written once, and the exact guard item and notice two lesson hits
produce. Written against the code before seam 5 and kept unchanged after it."""

import uuid
from datetime import UTC, datetime

import pytest

from app.jobs import service as jobs_service
from app.learning import service as learning
from app.learning import store


def _job(n, **overrides):
    job = {
        "id": uuid.UUID(int=n),
        "profile": "change",
        "title": f"kelsier: task {n}",
        "status": "succeeded",
        "error": None,
        "params": {"repo": "tharpep/kelsier"},
        "meta": {},
        "finished_at": datetime(2026, 11, n, tzinfo=UTC),
    }
    return job | overrides


@pytest.fixture
def fake(monkeypatch):
    state = {"classes": {}, "hits": [], "guards": [], "created": [], "notified": []}

    async def set_class(job_id, cls):
        if job_id in state["classes"]:
            return False
        state["classes"][job_id] = cls
        return True

    async def hits_since(cls, since, limit=50):
        return [h for h in state["hits"] if since is None or h["finished_at"] > since]

    async def guard_items(cls):
        return state["guards"]

    async def create_guard(**fields):
        state["created"].append(fields)
        return {"id": uuid.UUID(int=99), **fields}

    async def notify(kind, title, body, refs=None):
        state["notified"].append((kind, title, body, refs))

    monkeypatch.setattr(store, "set_class", set_class)
    monkeypatch.setattr(store, "hits_since", hits_since)
    monkeypatch.setattr(store, "guard_items", guard_items)
    monkeypatch.setattr(store, "create_guard", create_guard)
    monkeypatch.setattr(jobs_service, "notify_simple", notify)
    return state


@pytest.mark.parametrize(
    ("job", "expected"),
    [
        (_job(1, status="failed", error="Claude usage limit reached"), "quota_exhausted"),
        (_job(2, status="failed", error="worker error: clone failed"), "worker_error"),
        (_job(3, meta={"check_result": {"passed": False}}), "checks_failed"),
        (_job(4, status="timed_out", error="worker stopped reporting"), "worker_lost"),
        (_job(5, meta={"changed_files": 0}), "no_change"),
        (_job(6), None),
    ],
)
async def test_record_classes_a_job_as_before(fake, job, expected):
    assert await learning.record(job) == expected
    assert fake["classes"] == ({job["id"]: expected} if expected else {})


async def test_two_lesson_hits_give_the_same_guard_item_and_notice_as_before(fake):
    first = _job(3, status="failed", error="boom one")
    second = _job(4, status="failed", error="boom two", params={"repo": "tharpep/sazed"})
    fake["hits"] = [second, first]
    assert await learning.record(second) == "agent_error"
    assert fake["created"] == [
        {
            "title": "Guard: Agent error",
            "summary": (
                'Goal: stop jobs from ending as "Agent error" (agent_error).\n\n'
                "Today: this class has been hit 2 times since the last guard proposal:\n"
                "- 00000000 kelsier: task 4: boom two (2026-11-04)\n"
                "- 00000000 kelsier: task 3: boom one (2026-11-03)\n\n"
                "Change: add a test or harness check on the agent's failing step, so the error is "
                "caught early. Prefer a harness check or test over a prompt rule. Write the guard "
                "as When / Do not / Unless / Instead.\n\n"
                "First: read the evidence jobs and confirm they share one cause.\n\n"
                "Done when: a job like the evidence jobs is stopped or fixed by the guard, pinned "
                "by a test."
            ),
            "why": "agent_error hit 2 times since the last guard proposal.",
            "repos": ["tharpep/kelsier", "tharpep/sazed"],
            "sources": [
                "failure_class:agent_error",
                f"job:{uuid.UUID(int=4)}",
                f"job:{uuid.UUID(int=3)}",
            ],
        }
    ]
    assert fake["notified"] == [
        (
            "routine",
            "Guard proposed: Agent error",
            "agent_error hit 2 times; the guard item is in your board inbox.",
            {"card": str(uuid.UUID(int=99))},
        )
    ]


async def test_an_open_guard_or_a_stop_proposes_nothing_as_before(fake):
    fake["hits"] = [_job(4, status="failed", error="b"), _job(3, status="failed", error="a")]
    fake["guards"] = [{"status": "new", "created_at": datetime(2026, 11, 1, tzinfo=UTC)}]
    assert await learning.record(_job(4, status="failed", error="b")) == "agent_error"
    fake["guards"] = []
    assert await learning.record(_job(7, status="cancelled")) == "cancelled"
    assert fake["created"] == [] and fake["notified"] == []
