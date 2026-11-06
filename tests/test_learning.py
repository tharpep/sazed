"""Learning v1: failure classes from code rules, and one guard proposal on a lesson's second hit."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.config import settings
from app.jobs import service as jobs_service
from app.learning import service as learning
from app.learning import store
from app.learning.classes import CLASSES, classify
from app.routers import worker

JOB_ID = uuid.UUID("88888888-8888-8888-8888-888888888888")


def _job(**overrides):
    job = {
        "id": JOB_ID,
        "profile": "change",
        "title": "kelsier: retry sync",
        "status": "succeeded",
        "error": None,
        "params": {"repo": "tharpep/kelsier"},
        "meta": {},
        "finished_at": datetime(2026, 11, 4, tzinfo=UTC),
    }
    return job | overrides


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"status": "cancelled"}, "cancelled"),
        ({"status": "failed", "meta": {"stop_reason": "timed_out"}}, "timed_out"),
        ({"status": "paused", "meta": {"pause_reason": "tool_cap"}}, "tool_cap"),
        ({"status": "paused", "meta": {"pause_reason": "rate_limit"}}, "rate_limit"),
        ({"status": "failed", "meta": {"stop_reason": "shutdown"}}, "shutdown"),
        ({"status": "timed_out", "error": "hit the job timeout"}, "timed_out"),
        ({"status": "timed_out", "error": "worker stopped reporting"}, "worker_lost"),
        ({"status": "failed", "error": "Claude usage limit: quota reached"}, "quota_exhausted"),
        ({"status": "failed", "error": "worker error: clone failed"}, "worker_error"),
        ({"status": "failed", "error": "boom"}, "agent_error"),
        ({"status": "failed", "error": None}, "agent_error"),
        ({"meta": {"pr_review_error": "422"}}, "review_post_failed"),
        ({"meta": {"pr_error": "Reference does not exist"}}, "pr_gone"),
        ({"meta": {"pr_error": "PR is closed, not reopening"}}, "pr_gone"),
        ({"meta": {"pr_error": "502 bad gateway"}}, "pr_open_failed"),
        ({"meta": {"check_result": {"passed": False}}}, "checks_failed"),
        ({"meta": {"ci_state": "failure"}}, "ci_failed"),
        ({"meta": {"result_format": "prose"}}, "result_unusable"),
        ({"meta": {"changed_files": 0}}, "no_change"),
        ({"status": "timed_out", "error": "something new"}, "unclassified"),
    ],
)
def test_each_rule_picks_its_class(overrides, expected):
    assert classify(_job(**overrides)) == expected


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"status": "cancelled", "meta": {"pr_error": "closed"}}, "cancelled"),
        ({"status": "failed", "error": "x", "meta": {"stop_reason": "tool_cap"}}, "tool_cap"),
        ({"meta": {"check_result": {"passed": False}, "ci_state": "failure"}}, "checks_failed"),
        ({"meta": {"pr_error": "closed", "check_result": {"passed": False}}}, "pr_gone"),
        ({"meta": {"ci_state": "failure", "result_format": "prose"}}, "ci_failed"),
    ],
)
def test_the_earliest_fault_wins(overrides, expected):
    assert classify(_job(**overrides)) == expected


@pytest.mark.parametrize(
    "overrides",
    [
        {},
        {"meta": {"changed_files": 3, "check_result": {"passed": True}}},
        {"meta": {"changed_files": 0}, "params": {"result_only": True}},
        {"profile": "review", "meta": {"changed_files": 0}},
        {"status": "needs_input", "meta": {"question": "which?"}},
        {"status": "queued"},
        {"status": "running"},
        {"status": "needs_approval"},
    ],
)
def test_a_job_that_did_not_go_wrong_has_no_class(overrides):
    assert classify(_job(**overrides)) is None


def test_lesson_classes_carry_a_guard_hint_and_stop_classes_do_not():
    lessons = [n for n, c in CLASSES.items() if c.family == "lesson" and n != "unclassified"]
    assert all(CLASSES[n].guard_hint for n in lessons)
    assert not any(c.guard_hint for c in CLASSES.values() if c.family == "stop")


@pytest.fixture
def db(monkeypatch):
    state = {
        "classes": {},
        "hits": [],
        "guards": [],
        "created": [],
        "notified": [],
        "since": [],
        "unset": [],
    }

    async def set_class(job_id, cls):
        if job_id in state["classes"]:
            return False
        state["classes"][job_id] = cls
        return True

    async def hits_since(cls, since, limit=50):
        state["since"].append(since)
        return [h for h in state["hits"] if since is None or h["finished_at"] > since]

    async def guard_items(cls):
        return state["guards"]

    async def create_guard(**fields):
        state["created"].append(fields)
        return {"id": uuid.uuid4(), **fields}

    async def unset_settled():
        return state["unset"]

    async def notify(title, body, url=None, url_title=None):
        state["notified"].append((title, url))

    monkeypatch.setattr(store, "set_class", set_class)
    monkeypatch.setattr(store, "hits_since", hits_since)
    monkeypatch.setattr(store, "guard_items", guard_items)
    monkeypatch.setattr(store, "create_guard", create_guard)
    monkeypatch.setattr(store, "unset_settled", unset_settled)
    monkeypatch.setattr(jobs_service, "notify_simple", notify)
    return state


def _hit(n, repo="tharpep/kelsier", days_ago=1):
    return _job(
        id=uuid.UUID(int=n),
        status="failed",
        error=f"boom {n}",
        params={"repo": repo},
        finished_at=datetime.now(UTC) - timedelta(days=days_ago),
    )


async def test_record_sets_the_class_only_while_it_is_null(db):
    assert await learning.record(_job(status="cancelled")) == "cancelled"
    assert db["classes"] == {JOB_ID: "cancelled"}
    await learning.record(_job(status="failed", error="x"))
    assert db["classes"] == {JOB_ID: "cancelled"}


async def test_record_returns_none_for_a_clean_job(db):
    assert await learning.record(_job()) is None
    assert db["classes"] == {}


async def test_record_never_raises(db, monkeypatch):
    async def broken(job_id, cls):
        raise RuntimeError("db down")

    monkeypatch.setattr(store, "set_class", broken)
    assert await learning.record(_job(status="failed", error="x")) is None


async def test_the_first_hit_proposes_nothing(db):
    db["hits"] = [_hit(1)]
    await learning.record(_hit(1))
    assert db["created"] == []


async def test_the_second_hit_opens_one_guard_item_and_notifies(db):
    db["hits"] = [_hit(2, "tharpep/sazed"), _hit(1)]
    await learning.record(_hit(2, "tharpep/sazed"))
    (item,) = db["created"]
    assert item["title"] == "Guard: Agent error"
    assert item["repos"] == ["tharpep/kelsier", "tharpep/sazed"]
    assert item["sources"][0] == "failure_class:agent_error"
    assert item["sources"][1:] == [f"job:{uuid.UUID(int=2)}", f"job:{uuid.UUID(int=1)}"]
    for heading in ("Goal:", "Today:", "Change:", "First:", "Done when:"):
        assert heading in item["summary"]
    assert "boom 2" in item["summary"]
    assert db["notified"] == [("Guard proposed: Agent error", settings.console_url)]


@pytest.mark.parametrize("status", ["new", "shortlisted", "approved"])
async def test_an_open_guard_item_blocks_a_second_proposal(db, status):
    db["hits"] = [_hit(1), _hit(2)]
    db["guards"] = [{"status": status, "created_at": datetime.now(UTC) - timedelta(days=9)}]
    await learning.maybe_propose("agent_error")
    assert db["created"] == []


async def test_only_hits_after_the_newest_guard_item_count(db):
    created_at = datetime.now(UTC) - timedelta(days=3)
    db["guards"] = [{"status": "done", "created_at": created_at}]
    db["hits"] = [_hit(1, days_ago=5), _hit(2, days_ago=2)]
    await learning.maybe_propose("agent_error")
    assert db["since"] == [created_at]
    assert db["created"] == []
    db["hits"].append(_hit(3, days_ago=1))
    await learning.maybe_propose("agent_error")
    assert len(db["created"]) == 1


@pytest.mark.parametrize(
    "job",
    [
        _job(status="cancelled"),
        _job(status="failed", meta={"stop_reason": "tool_cap"}),
        _job(status="timed_out", error="something new"),
    ],
)
async def test_stop_classes_and_unclassified_never_propose(db, job):
    db["hits"] = [_hit(1), _hit(2), _hit(3)]
    await learning.record(job)
    assert db["created"] == []


async def test_backfill_sets_classes_and_proposes_nothing(db):
    db["hits"] = [_hit(1), _hit(2)]
    db["unset"] = [_hit(1), _hit(2), _job(), _job(id=uuid.UUID(int=9), status="cancelled")]
    assert await learning.backfill() == 3
    assert db["classes"][uuid.UUID(int=9)] == "cancelled"
    assert db["created"] == [] and db["notified"] == []


async def test_finish_records_the_changed_file_count_for_a_change_job(monkeypatch):
    seen = {}

    async def fake_finish_job(job_id, status, result, error, session_id, meta):
        seen["meta"] = meta
        return _job(status=status, meta=meta)

    async def fake_get_job(job_id):
        return _job(status="running")

    async def fake_land(job, result, changes, draft=False):
        return result, {}

    async def fake_on_finished(job):
        return None

    monkeypatch.setattr(worker.store, "finish_job", fake_finish_job)
    monkeypatch.setattr(worker.store, "get_job", fake_get_job)
    monkeypatch.setattr(worker, "_land_changes", fake_land)
    monkeypatch.setattr(worker.service, "on_finished", fake_on_finished)
    body = worker.FinishRequest(status="succeeded", result="Done.", changes={"a.py": "eA=="})
    await worker.finish(JOB_ID, body)
    assert seen["meta"]["changed_files"] == 1
    await worker.finish(JOB_ID, worker.FinishRequest(status="succeeded", result="Done."))
    assert seen["meta"]["changed_files"] == 0
