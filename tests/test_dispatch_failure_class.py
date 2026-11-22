"""Seam 5: a red dispatch row gets a failure class from dispatch_failure_class (migration 038), the
one mapping, and feeds the same guard proposals as jobs. CI has no Postgres, so the mapping tests
read the function's text; S5-8 ran it against a real database. WORKER_REASONS and RED_STATES copy
REASONS and NEEDS_REASON from sazed-worker's sazed_worker/dispatch/reasons.py: a code added there
needs a branch in dispatch_failure_class and a line here."""

import re
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest
from fastapi import BackgroundTasks

from app.jobs import service as jobs_service
from app.learning import service as learning
from app.learning import store
from app.learning.classes import CLASSES
from app.migrations import _discover
from app.routers import dispatch as router
from tests.conftest import REAL_CLASS_LOCK

WORKER_REASONS = {
    "timeout",
    "budget",
    "plan_limit",
    "tool_cap",
    "refused",
    "max_tokens",
    "cancelled",
    "no_result",
    "no_done_state",
    "no_evidence",
    "no_change",
    "wrapper_failed",
    "left_owns",
    "check_failed",
    "regen_failed",
    "conflict",
    "dependency_failed",
    "dependency_pending",
    "overlap",
}
RED_STATES = {"failed", "blocked", "conflict", "timed_out"}

MAPPING = {
    "timeout": "timed_out",
    "plan_limit": "rate_limit",
    "tool_cap": "tool_cap",
    "cancelled": "cancelled",
    "budget": "budget",
    "no_change": "no_change",
    "check_failed": "checks_failed",
    "no_result": "agent_error",
    "no_done_state": "result_unusable",
    "max_tokens": "result_unusable",
    "wrapper_failed": "worker_error",
    "regen_failed": "worker_error",
    "left_owns": "left_owns",
    "conflict": "land_conflict",
    "no_evidence": "no_evidence",
    "refused": "refused",
    "dependency_pending": None,
    "dependency_failed": None,
    "overlap": None,
}


def _function() -> str:
    """The newest migration's body of dispatch_failure_class."""
    for _, _, path in reversed(_discover()):
        sql = path.read_text(encoding="utf-8")
        if "FUNCTION dispatch_failure_class(" in sql:
            return sql.split("FUNCTION dispatch_failure_class(", 1)[1].split("$$;", 1)[0]
    raise AssertionError("no migration defines dispatch_failure_class")


def _branches(body: str) -> dict[str, str | None]:
    """Fails on a code with two branches, since SQL would take the first and a dict the last."""
    pairs = re.findall(r"WHEN '(\w+)' THEN (?:'(\w+)'|NULL)", body)
    codes = [code for code, _ in pairs]
    dupes = {c for c in codes if codes.count(c) > 1}
    assert not dupes, f"dispatch_failure_class has more than one branch for {dupes}"
    return {code: cls or None for code, cls in pairs}


def test_a_duplicate_branch_fails_the_guard():
    with pytest.raises(AssertionError, match="more than one branch"):
        _branches("WHEN 'timeout' THEN 'timed_out' WHEN 'timeout' THEN 'budget'")


def test_every_worker_reason_and_the_reapers_has_a_branch():
    body = _function()
    missing = WORKER_REASONS - set(_branches(body))
    assert not missing, f"dispatch_failure_class has no branch for {missing}"
    assert "WHEN 'no_heartbeat' IN (meta->>'reason', meta->>'stop_reason') THEN 'worker_lost'" in (
        body
    )


def test_every_returned_class_is_in_sazeds_list():
    returned = set(re.findall(r"(?:THEN|ELSE) '(\w+)'", _function()))
    assert returned, "no returned classes found"
    assert returned <= set(CLASSES), f"outside the class list: {returned - set(CLASSES)}"


def test_the_mapping_is_the_one_the_design_lists():
    body = _function()
    assert _branches(body) == MAPPING
    assert re.search(r"ELSE 'unclassified'\s+END", body)


def test_only_a_red_row_has_a_class():
    (states,) = re.findall(r"WHEN raw IN \(([^)]*)\) THEN", _function())
    assert set(re.findall(r"'(\w+)'", states)) == RED_STATES


def test_new_dispatch_classes_sit_in_their_families():
    assert CLASSES["budget"].family == "stop"
    for name in ("left_owns", "land_conflict", "no_evidence", "refused"):
        assert CLASSES[name].family == "lesson" and CLASSES[name].guard_hint


def test_run_rows_task_branch_carries_the_class_of_its_latest_uncleaned_row():
    sql = next(p for _, _, p in _discover() if p.name.startswith("038_")).read_text("utf-8")
    view = sql.split("CREATE OR REPLACE VIEW run_rows AS", 1)[1]
    assert (
        "CASE WHEN l.state = 'cleaned' THEN dispatch_failure_class(b.state, b.meta)\n"
        "        ELSE dispatch_failure_class(l.state, l.meta) END AS failure_class"
    ) in view


@pytest.fixture
def ledger(monkeypatch):
    state = {"cls": None, "proposed": [], "guards": [], "hits": [], "created": []}

    async def dispatch_class(st, meta):
        if isinstance(state["cls"], Exception):
            raise state["cls"]
        return state["cls"]

    async def maybe_propose(cls):
        state["proposed"].append(cls)

    monkeypatch.setattr(store, "dispatch_class", dispatch_class)
    monkeypatch.setattr(learning, "maybe_propose", maybe_propose)
    return state


@pytest.mark.parametrize(
    ("cls", "proposes"),
    [
        ("checks_failed", True),
        ("land_conflict", True),
        ("worker_lost", False),
        ("budget", False),
        ("unclassified", False),
        (None, False),
    ],
)
async def test_a_red_row_proposes_only_for_a_lesson(ledger, cls, proposes):
    ledger["cls"] = cls
    assert await learning.record_dispatch("p", "t", "failed", {"reason": "x"}) == cls
    assert ledger["proposed"] == ([cls] if proposes else [])


async def test_recording_a_row_never_raises(ledger):
    ledger["cls"] = RuntimeError("db down")
    assert await learning.record_dispatch("p", "t", "failed", {"reason": "no_result"}) is None


async def test_row_intake_leaves_learning_to_a_background_task(monkeypatch):
    stored, recorded = [], []

    async def insert_row(**row):
        stored.append(row)

    async def record_dispatch(*args):
        recorded.append(args)

    monkeypatch.setattr(router.dispatch, "insert_row", insert_row)
    monkeypatch.setattr(learning, "record_dispatch", record_dispatch)
    body = router.RowIn(plan="p", task="t", ts=1.0, state="failed", meta={"reason": "check_failed"})
    background = BackgroundTasks()
    assert await router.post_row(body, background) == {"ok": True}
    assert len(stored) == 1 and recorded == []
    await background()
    assert recorded == [("p", "t", "failed", {"reason": "check_failed"})]


async def test_job_and_dispatch_hits_add_up_to_one_guard_naming_both(monkeypatch):
    job = {
        "id": uuid.UUID(int=1),
        "title": "kelsier: task",
        "status": "failed",
        "error": "boom",
        "params": {"repo": "tharpep/kelsier"},
        "meta": {},
        "finished_at": datetime(2026, 11, 3, tzinfo=UTC),
    }
    task = {
        "plan": "seam-5",
        "task": "api",
        "line": "agent gave no result",
        "reason": "no_result",
        "repo": "tharpep/sazed",
        "finished_at": datetime(2026, 11, 4, tzinfo=UTC),
    }
    created, notified = [], []

    async def guard_items(cls):
        return []

    async def hits_since(cls, since, limit=50):
        return [task, job]

    async def create_guard(**fields):
        created.append(fields)
        return {"id": 1, **fields}

    async def notify(*args):
        notified.append(args)

    monkeypatch.setattr(store, "guard_items", guard_items)
    monkeypatch.setattr(store, "hits_since", hits_since)
    monkeypatch.setattr(store, "create_guard", create_guard)
    monkeypatch.setattr(jobs_service, "notify_simple", notify)
    await learning.maybe_propose("agent_error")
    (item,) = created
    assert item["sources"] == [
        "failure_class:agent_error",
        "dispatch:seam-5:api",
        f"job:{uuid.UUID(int=1)}",
    ]
    assert item["repos"] == ["tharpep/kelsier", "tharpep/sazed"]
    assert "- dispatch seam-5/api: agent gave no result (2026-11-04)" in item["summary"]
    assert len(notified) == 1


async def test_a_guard_with_dispatch_evidence_speaks_of_runs_and_a_job_only_one_of_jobs(
    monkeypatch,
):
    task = {
        "plan": "p",
        "task": "t",
        "line": "no result",
        "finished_at": datetime(2026, 11, 4, tzinfo=UTC),
    }
    summary = learning._summary("agent_error", [task, task])
    assert summary.startswith('Goal: stop runs from ending as "Agent error"')
    assert "First: read the evidence runs and confirm" in summary
    assert "Done when: a run like the evidence runs is stopped" in summary
    assert "jobs" not in summary


async def test_the_proposal_reads_and_creates_under_the_class_lock_and_notifies_after(
    monkeypatch,
):
    events = []

    @asynccontextmanager
    async def class_lock(cls):
        events.append(f"lock {cls}")
        yield
        events.append("commit")

    async def guard_items(cls):
        events.append("guards")
        return []

    async def hits_since(cls, since, limit=50):
        events.append("hits")
        task = {"plan": "p", "task": "t", "finished_at": datetime(2026, 11, 4, tzinfo=UTC)}
        return [task, {**task, "plan": "q"}]

    async def create_guard(**fields):
        events.append("create")
        return {"id": 1, **fields}

    async def notify(*args):
        events.append("notify")

    monkeypatch.setattr(store, "class_lock", class_lock)
    monkeypatch.setattr(store, "guard_items", guard_items)
    monkeypatch.setattr(store, "hits_since", hits_since)
    monkeypatch.setattr(store, "create_guard", create_guard)
    monkeypatch.setattr(jobs_service, "notify_simple", notify)
    await learning.maybe_propose("land_conflict")
    assert events == ["lock land_conflict", "guards", "hits", "create", "commit", "notify"]


async def test_the_class_lock_is_a_transaction_advisory_lock_used_by_the_guard_queries(
    monkeypatch,
):
    seen = []

    class Tx:
        async def __aenter__(self):
            seen.append("begin")

        async def __aexit__(self, *exc):
            seen.append("commit")

    class Conn:
        def transaction(self):
            return Tx()

        async def execute(self, sql, *args):
            seen.append((" ".join(sql.split()), args))

        async def fetch(self, sql, *args):
            seen.append("fetch on the locked connection")
            return []

    class Acquire:
        async def __aenter__(self):
            return Conn()

        async def __aexit__(self, *exc):
            return None

    class Pool:
        def acquire(self):
            return Acquire()

    monkeypatch.setattr(store, "get_pool", lambda: Pool())
    async with REAL_CLASS_LOCK("agent_error"):
        await store.guard_items("agent_error")
    assert seen == [
        "begin",
        (
            "SELECT pg_advisory_xact_lock(hashtext('learning_guard'), hashtext($1))",
            ("agent_error",),
        ),
        "fetch on the locked connection",
        "commit",
    ]


LATEST_RED_TASKS = (
    "SELECT l.plan, l.task, l.line, l.meta->>'reason' AS reason, to_timestamp(l.ts) AS "
    "finished_at, dispatch_failure_class(l.state, l.meta) AS cls FROM ( SELECT DISTINCT ON "
    "(plan, task) plan, task, state, line, meta, ts FROM dispatch_rows WHERE state <> 'cleaned' "
    "ORDER BY plan, task, ts DESC, id DESC ) l WHERE dispatch_failure_class(l.state, l.meta) IS "
    "NOT NULL"
)
HITS_SQL = [
    "SELECT * FROM jobs WHERE failure_class = $1 AND ($2::timestamptz IS NULL OR finished_at > "
    "$2) ORDER BY finished_at DESC LIMIT $3",
    "SELECT DISTINCT ON (h.plan) h.plan, h.task, h.line, h.reason, h.finished_at, p.repo FROM ( "
    f"{LATEST_RED_TASKS} ) h LEFT JOIN dispatch_plans p ON p.plan = h.plan WHERE h.cls = $1 AND "
    "($2::timestamptz IS NULL OR h.finished_at > $2) ORDER BY h.plan, h.finished_at DESC",
]
COUNTS_SQL = (
    "SELECT cls, count(*) AS total, count(*) FILTER (WHERE finished_at > NOW() - INTERVAL "
    "'30 days') AS last_30d FROM ( SELECT failure_class AS cls, finished_at FROM jobs WHERE "
    f"failure_class IS NOT NULL UNION ALL SELECT cls, finished_at FROM ( {LATEST_RED_TASKS} ) t "
    ") hits GROUP BY cls"
)


def _capture(monkeypatch, rows_for):
    calls = []

    class Pool:
        async def fetch(self, sql, *args):
            calls.append((" ".join(sql.split()), args))
            return rows_for(sql)

    monkeypatch.setattr(store, "get_pool", lambda: Pool())
    monkeypatch.setattr(store, "_jobs", lambda rows: list(rows))
    return calls


async def test_hits_since_runs_the_sql_verified_on_postgres_and_merges_newest_first(monkeypatch):
    """The text S5-8 ran on Postgres 18.6; a change here needs a new run there."""
    job = {"id": uuid.UUID(int=1), "finished_at": datetime(2026, 11, 3, tzinfo=UTC)}
    task = {"plan": "p", "task": "t", "finished_at": datetime(2026, 11, 4, tzinfo=UTC)}
    calls = _capture(monkeypatch, lambda sql: [job] if "FROM jobs" in sql else [task])
    assert await store.hits_since("agent_error", None) == [task, job]
    assert calls == [
        (HITS_SQL[0], ("agent_error", None, 50)),
        (HITS_SQL[1], ("agent_error", None)),
    ]


async def test_class_counts_runs_the_sql_verified_on_postgres(monkeypatch):
    """The text S5-8 ran on Postgres 18.6; a change here needs a new run there."""
    calls = _capture(monkeypatch, lambda sql: [{"cls": "agent_error", "total": 2, "last_30d": 1}])
    assert await store.class_counts() == [{"cls": "agent_error", "total": 2, "last_30d": 1}]
    assert calls == [(COUNTS_SQL, ())]
