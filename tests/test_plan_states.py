"""Plan states (slice 4, migration 041): approve plan, start, withdraw, the stage texts, and the
additive card, command and session fields. Checks S1-S8 in My-AI
plans/board-visibility-2026-11-19/slice-4-checks.md."""

import inspect
import itertools
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.backlog import service, store
from app.board import command, events, stages
from app.board import service as board
from app.routers import backlog as router
from app.sessions import service as sessions

ITEM = uuid.UUID("11111111-2222-3333-4444-555555555555")
MIGRATION = Path(__file__).parent.parent / "app" / "migrations" / "041_plan_states.sql"


class Card:
    """One backlog row's plan state, changed only through the store and events seams."""

    PLANS = {"pl": uuid.UUID(int=1), "other": uuid.UUID(int=2)}

    def __init__(self, status="new", plan="pl", section="build", rank=1, changed=False):
        self.status = status
        self.rank = rank
        self.plan = plan
        self.section = section if plan else None
        self.plan_state = "active"
        self.changed = changed
        self.cols = dict.fromkeys(store.PLAN_STATE_COLUMNS)
        self.events: list[tuple] = []
        self.writes = 0

    def link(self, plan, section=None):
        self.plan, self.section = plan, section

    def state(self):
        slugs = {v: k for k, v in self.PLANS.items()}
        row = self.cols | {
            "status": self.status,
            "rank": self.rank,
            "workstream_id": None,
            "plan_id": self.PLANS.get(self.plan),
            "plan_section": self.section,
            "plan_slug": self.plan,
            "plan_state": self.plan_state if self.plan else None,
            "plan_active": self.plan_state == "active",
            "plan_current_version": 4 if self.plan else None,
            "approved_plan_slug": slugs.get(self.cols["plan_approved_plan_id"]),
        }
        edited = self.changed and bool(self.cols["plan_approved_at"])
        return row | {"plan_changed": edited or events.stale(row)}


class FakeConn:
    def acquire(self):
        return self

    def transaction(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


@pytest.fixture
def card(monkeypatch):
    found = Card()

    async def lock_status(conn, item_id):
        return found.status if item_id == ITEM else None

    async def set_plan_state(conn, item_id, **fields):
        assert set(fields) <= set(store.PLAN_STATE_COLUMNS)
        found.cols |= fields
        found.writes += 1

    async def state(item_id, db=None):
        return found.state()

    async def record(db, item_id, kind, actor, detail=None):
        assert kind in events.KINDS
        found.events.append((kind, actor, detail or {}))

    async def no_job(*args, **kwargs):
        raise AssertionError("plan states never create a job")

    monkeypatch.setattr(service, "get_pool", FakeConn)
    monkeypatch.setattr(store, "lock_status", lock_status)
    monkeypatch.setattr(store, "set_plan_state", set_plan_state)
    monkeypatch.setattr(events, "state", state)
    monkeypatch.setattr(events, "record", record)
    monkeypatch.setattr(service.jobs, "create_code_task", no_job)
    return found


# S1: the migration


def _statements():
    sql = re.sub(r"--[^\n]*", "", MIGRATION.read_text(encoding="utf-8"))
    return [s.strip() for s in sql.split(";") if s.strip()]


def test_the_migration_is_idempotent_and_every_new_column_is_nullable():
    """Every statement guards with IF NOT EXISTS and no column is NOT NULL (the id is the primary
    key). The SQL was also run twice on a real Postgres; the PR body says how."""
    statements = _statements()
    assert len(statements) == 4
    for s in statements:
        adds = s.count("ADD COLUMN")
        assert adds == s.count("ADD COLUMN IF NOT EXISTS")
        if s.startswith("CREATE"):
            assert s.startswith(("CREATE TABLE IF NOT EXISTS", "CREATE INDEX IF NOT EXISTS"))
        assert "NOT NULL" not in s
    text = " ".join(statements)
    for column in (*store.PLAN_STATE_COLUMNS, "role"):
        assert f"ADD COLUMN IF NOT EXISTS {column} " in text
    for column in ("item_id", "at", "kind", "actor", "detail"):
        assert re.search(rf"\b{column}\s+\w+", text)


def test_the_migration_needs_nothing_from_040():
    """040 (sazed#214) adds dispatch_plans.job_id; 041 must apply on a database without it."""
    text = " ".join(_statements())
    assert "dispatch_plans" not in text and "job_id" not in text


# S2: approve plan


async def test_approve_plan_records_who_when_and_the_plan_version_and_creates_no_job(card):
    assert await service.approve_plan(ITEM, "agent:orch") is True
    assert card.cols["plan_approved_by"] == "agent:orch"
    assert card.cols["plan_approved_version"] == 4
    assert isinstance(card.cols["plan_approved_at"], datetime)
    assert card.cols["started_at"] is None
    assert card.events == [
        (
            "plan_approved",
            "agent:orch",
            {"plan": "pl", "section": "build", "version": 4, "replaces_stale": False},
        )
    ]


async def test_a_second_approval_of_a_current_plan_changes_nothing(card):
    await service.approve_plan(ITEM, "pryce")
    writes, rows = card.writes, len(card.events)
    assert await service.approve_plan(ITEM, "agent:orch") is False
    assert (card.writes, len(card.events)) == (writes, rows)
    assert card.cols["plan_approved_by"] == "pryce"


async def test_a_stale_approval_is_replaced(card):
    await service.approve_plan(ITEM, "pryce")
    card.changed = True
    assert await service.approve_plan(ITEM, "agent:orch") is True
    assert card.events[-1][2]["replaces_stale"] is True


async def test_a_card_with_no_plan_link_can_be_approved(card):
    card.link(None)
    assert await service.approve_plan(ITEM, "pryce") is True
    assert card.cols["plan_approved_version"] is None
    assert card.events[0][2] == {
        "plan": None,
        "section": None,
        "version": None,
        "replaces_stale": False,
    }


@pytest.mark.parametrize("status", ["done", "dismissed", "stale", "parked"])
async def test_a_card_off_the_board_takes_no_plan_state(card, status):
    card.status = status
    with pytest.raises(service.BacklogError):
        await service.approve_plan(ITEM, "pryce")


async def test_an_unknown_card_is_not_found(card):
    with pytest.raises(service.NotFound):
        await service.approve_plan(uuid.uuid4(), "pryce")


async def test_an_approval_records_the_plan_and_section_it_approved(card):
    await service.approve_plan(ITEM, "pryce")
    assert card.cols["plan_approved_plan_id"] == Card.PLANS["pl"]
    assert card.cols["plan_approved_section"] == "build"
    assert card.state()["approved_plan_slug"] == "pl"


@pytest.mark.parametrize(
    "plan, section",
    [("other", "build"), ("other", "alpha"), ("pl", "gamma"), ("pl", None), (None, None)],
)
async def test_moving_or_unlinking_the_card_makes_its_approval_stale(card, plan, section):
    await service.approve_plan(ITEM, "pryce")
    card.link(plan, section)
    assert card.state()["plan_changed"] is True
    with pytest.raises(service.BacklogError, match="changed since approval"):
        await service.start(ITEM, "pryce")
    assert await service.approve_plan(ITEM, "pryce") is True
    assert card.state()["plan_changed"] is False
    assert card.cols["plan_approved_section"] == section


async def test_linking_a_plan_after_a_plan_less_approval_makes_it_stale(card):
    card.link(None)
    await service.approve_plan(ITEM, "pryce")
    assert card.state()["plan_changed"] is False
    card.link("pl", "build")
    assert card.state()["plan_changed"] is True
    with pytest.raises(service.BacklogError):
        await service.start(ITEM, "pryce")


def test_the_stale_rule_compares_the_approved_link_then_the_section_text():
    approved = {
        "plan_approved_at": "t",
        "plan_approved_plan_id": 1,
        "plan_approved_section": "build",
        "plan_id": 1,
        "plan_section": "build",
    }
    assert events.stale(approved) is False
    assert events.stale(approved | {"plan_id": 2}) is True
    assert events.stale(approved | {"plan_section": "other"}) is True
    assert events.stale(approved | {"plan_id": None, "plan_section": None}) is True
    edited = BODY.replace("Do it.", "Do more.")
    assert events.stale(approved | {"approved_body": BODY, "current_body": edited}) is True
    assert events.stale(approved | {"plan_approved_at": None, "plan_id": 2}) is False


async def test_an_agent_cannot_approve_an_inbox_card(card):
    card.rank = None
    with pytest.raises(service.BacklogError, match="inbox"):
        await service.approve_plan(ITEM, "agent:orch")
    assert card.writes == 0


async def test_pryce_approving_an_inbox_card_moves_it_to_next(card):
    card.rank = None
    assert stages.item_stage(card.state(), None)[0] == "inbox"
    assert await service.approve_plan(ITEM, "pryce") is True
    assert stages.item_stage(card.state(), None) == ("next", "Ready to start")


async def test_mark_stale_skips_approved_and_started_cards(monkeypatch):
    seen = []

    class Pool:
        async def execute(self, sql):
            seen.append(sql)
            return "UPDATE 0"

    monkeypatch.setattr(store, "get_pool", Pool)
    await store.mark_stale(30)
    assert "plan_approved_at IS NULL AND started_at IS NULL" in seen[0]


# S3: start


async def test_start_without_an_approval_is_refused(card):
    with pytest.raises(service.BacklogError, match="not approved"):
        await service.start(ITEM, "pryce")
    assert card.writes == 0


async def test_start_on_a_stale_approval_is_refused(card):
    await service.approve_plan(ITEM, "pryce")
    card.changed = True
    with pytest.raises(service.BacklogError, match="changed since approval"):
        await service.start(ITEM, "pryce")
    assert card.cols["started_at"] is None


@pytest.mark.parametrize("plan_state", ["done", "superseded"])
async def test_start_on_an_archived_plan_is_refused(card, plan_state):
    await service.approve_plan(ITEM, "pryce")
    card.plan_state = plan_state
    with pytest.raises(service.BacklogError, match=f"plan pl is {plan_state}"):
        await service.start(ITEM, "pryce")
    assert card.cols["started_at"] is None


async def test_start_on_a_draft_plan_is_allowed(card):
    await service.approve_plan(ITEM, "pryce")
    card.plan_state = "draft"
    assert await service.start(ITEM, "pryce") is True


async def test_start_records_who_and_when_once_and_creates_no_job(card):
    await service.approve_plan(ITEM, "pryce")
    assert await service.start(ITEM, "agent:orch") is True
    assert card.cols["started_by"] == "agent:orch"
    assert isinstance(card.cols["started_at"], datetime)
    assert card.events[-1] == ("started", "agent:orch", {})
    rows = len(card.events)
    assert await service.start(ITEM, "pryce") is False
    assert len(card.events) == rows and card.cols["started_by"] == "agent:orch"


async def test_start_and_approve_touch_no_job_or_dispatch_code():
    """The plan-state path calls no job or dispatch module: only the backlog store and events."""
    source = "".join(
        inspect.getsource(f)
        for f in (service.approve_plan, service.start, service.withdraw, service._start)
    )
    assert "jobs." not in source and "dispatch" not in source


# S4: the actor


@pytest.mark.parametrize("by", ["pryce", "agent:orch", "agent:ct113.orchestrator", "agent:a_b-9"])
def test_pryce_and_agents_are_actors(by):
    assert router.ActorRequest(by=by).by == by


@pytest.mark.parametrize(
    "by",
    [
        "",
        "Pryce",
        "agent:",
        "bob",
        "dispatch:run1",
        "pryce ",
        "pryce\n",
        "рryce",
        "agent:рр",
        "agent:é",
        "agent:１",
        "agent:Orch",
        "agent:x:y",
        "agent:x​",
        "agent:" + "a" * 65,
    ],
)
def test_anyone_else_is_refused(by):
    with pytest.raises(ValidationError):
        router.ActorRequest(by=by)


@pytest.fixture
def client(card, monkeypatch):
    async def one_card(card_id):
        return {"id": card_id}

    monkeypatch.setattr(router.board_service, "card", one_card)
    app = FastAPI()
    app.include_router(router.router, prefix="/backlog")
    return TestClient(app)


def test_the_routes_answer_422_for_an_unknown_actor_and_403_for_an_agent_withdrawing(client):
    for path in ("approve-plan", "start", "withdraw"):
        body = {"by": "someone"} | ({"step": "start"} if path == "withdraw" else {})
        assert client.post(f"/backlog/{ITEM}/{path}", json=body).status_code == 422
    agent = client.post(f"/backlog/{ITEM}/withdraw", json={"by": "agent:x", "step": "start"})
    assert agent.status_code == 403
    missing = client.post(f"/backlog/{uuid.uuid4()}/approve-plan", json={"by": "pryce"})
    assert missing.status_code == 404


@pytest.mark.parametrize("actor", ["pryce", "agent:orch"])
def test_every_transition_as_each_actor(client, card, actor):
    def post(path, **body):
        return client.post(f"/backlog/{ITEM}/{path}", json={"by": actor} | body)

    def next_text():
        return stages.item_stage({"status": "new", "rank": 1} | card.state(), None)[1]

    assert next_text() == "Plan to approve"
    assert post("start").status_code == 409
    reply = post("approve-plan")
    assert reply.status_code == 200
    assert reply.json() == {"changed": True, "card": {"id": f"item:{ITEM}"}}
    assert next_text() == "Ready to start"
    assert post("approve-plan").json()["changed"] is False
    assert post("start").json()["changed"] is True
    assert next_text() == "Started; waiting for an orchestrator"
    assert post("start").json()["changed"] is False
    withdrawn = post("withdraw", step="start")
    if actor == "pryce":
        assert withdrawn.json()["changed"] is True
        assert next_text() == "Ready to start"
        card.changed = True
        assert next_text() == "Plan changed since approval"
        assert post("start").status_code == 409
        assert post("withdraw", step="approval").json()["changed"] is True
        assert next_text() == "Plan to approve"
        assert post("withdraw", step="approval").json()["changed"] is False
    else:
        assert withdrawn.status_code == 403
        assert card.cols["started_by"] == actor
        card.changed = True
        assert next_text() == "Started; waiting for an orchestrator"
        assert post("approve-plan").json()["changed"] is True


async def test_withdrawing_an_approval_also_withdraws_its_start(card):
    await service.approve_plan(ITEM, "pryce")
    await service.start(ITEM, "pryce")
    assert await service.withdraw(ITEM, "pryce", "approval") is True
    assert all(v is None for v in card.cols.values())
    assert [e[0] for e in card.events[-2:]] == ["start_withdrawn", "approval_withdrawn"]


# S5: stage texts


_STATUSES = ("new", "shortlisted", "approved", "done")
_PLACES = ({}, {"rank": 1}, {"workstream_id": "w1"})
_LINKS = ({}, {"session_label": "cc-7"}, {"session_branch": "feature/x"}, {"pr_number": 12})
_APPROVAL = (
    {},
    {"plan_approved_at": "t"},
    {"plan_approved_at": "t", "plan_changed": True},
)
_STARTED = ({}, {"started_at": "t"})
_JOBS = (None, {"status": "running", "meta": {}})


def _every_item():
    for status, place, link, approval, started, job, reporting in itertools.product(
        _STATUSES, _PLACES, _LINKS, _APPROVAL, _STARTED, _JOBS, (False, True)
    ):
        item = {"status": status, "rank": None, "workstream_id": None} | place | link
        yield item | approval | started, job, reporting


def test_approve_it_to_start_appears_on_no_card():
    texts = set()
    for item, job, reporting in _every_item():
        texts.add(stages.item_stage(item, job, reporting=reporting)[1])
    assert "Approve it to start" not in texts
    assert {
        "Plan to approve",
        "Plan changed since approval",
        "Ready to start",
        "Started; waiting for an orchestrator",
    } <= texts


@pytest.mark.parametrize(
    "state, expected",
    [
        ({}, ("next", "Plan to approve")),
        ({"plan_approved_at": "t"}, ("next", "Ready to start")),
        ({"plan_approved_at": "t", "plan_changed": True}, ("next", "Plan changed since approval")),
        (
            {"plan_approved_at": "t", "started_at": "t"},
            ("doing", "Started; waiting for an orchestrator"),
        ),
    ],
)
def test_the_plan_texts(state, expected):
    assert stages.item_stage({"status": "new", "rank": 2} | state, None) == expected


def test_a_session_job_or_pr_wins_over_the_plan_texts():
    started = {"status": "new", "rank": 2, "plan_approved_at": "t", "started_at": "t"}
    assert (
        stages.item_stage(started, None, reporting=True)[1] == "Being built in a terminal session"
    )
    linked = started | {"session_branch": "feature/x"}
    assert stages.item_stage(linked, None)[1] == "Being built in a terminal session"
    with_pr = started | {"pr_number": 3}
    assert stages.item_stage(with_pr, None)[1] == "Being built in a terminal session"
    job = {"status": "running", "meta": {}}
    assert stages.item_stage(started | {"status": "approved"}, job) == ("doing", "Running")


def test_a_reporting_session_alone_does_not_move_a_card_that_was_not_started():
    assert stages.item_stage({"status": "new", "rank": 2}, None, reporting=True)[1] == (
        "Plan to approve"
    )


# S6: the old one-click approve


async def test_the_old_approve_still_creates_its_job_and_records_approval_and_start(
    card, monkeypatch
):
    created = []

    async def create_code_task(repo, instruction, mode, requested_by, model=None):
        created.append((repo, mode, requested_by))
        return {"id": "job-1"}

    async def get_item(item_id):
        return {"id": item_id, "status": "new", "title": "T", "summary": "", "why": ""}

    async def update_item(item_id, **fields):
        return {"id": item_id, **fields}

    monkeypatch.setattr(service.jobs, "create_code_task", create_code_task)
    monkeypatch.setattr(store, "get_item", get_item)
    monkeypatch.setattr(store, "update_item", update_item)
    result = await service.approve(ITEM, "tharpep/sazed")
    assert created == [("tharpep/sazed", "change", "console")]
    assert result["status"] == "approved" and result["job_id"] == "job-1"
    assert (card.cols["plan_approved_by"], card.cols["started_by"]) == ("pryce", "pryce")
    assert [e[0] for e in card.events] == ["plan_approved", "started"]
    assert card.events[1][2] == {"via": "approve", "job_id": "job-1"}


async def test_the_old_approve_keeps_an_approval_and_start_already_recorded(card, monkeypatch):
    await service.approve_plan(ITEM, "agent:orch")
    await service.start(ITEM, "agent:orch")
    rows = len(card.events)

    async def create_code_task(*args, **kwargs):
        return {"id": "job-1"}

    async def get_item(item_id):
        return {"id": item_id, "status": "new", "title": "T", "summary": "", "why": ""}

    async def update_item(item_id, **fields):
        return {"id": item_id, **fields}

    monkeypatch.setattr(service.jobs, "create_code_task", create_code_task)
    monkeypatch.setattr(store, "get_item", get_item)
    monkeypatch.setattr(store, "update_item", update_item)
    await service.approve(ITEM, "tharpep/sazed")
    assert len(card.events) == rows
    assert (card.cols["plan_approved_by"], card.cols["started_by"]) == ("agent:orch", "agent:orch")


# S7: additive fields


def _item(**fields):
    now = datetime.now(UTC)
    base = {
        "id": ITEM,
        "type": "planned",
        "status": "new",
        "title": "Build it",
        "repos": [],
        "effort": None,
        "impact": None,
        "rank": 1,
        "job_id": None,
        "workstream_id": None,
        "plan_id": None,
        "plan_section": None,
        "plan_version": None,
        "session_label": None,
        "session_branch": None,
        "pr_repo": None,
        "pr_number": None,
        "created_at": now,
        "updated_at": now,
        "decided_at": None,
    }
    return base | fields


_OLD_CARD_KEYS = {
    "id", "kind", "source", "title", "stage", "next", "updated_at", "done_at", "repo",
    "item_type", "effort", "impact", "rank", "workstream_id", "plan_id", "plan_section",
    "plan_version", "session_link", "actions", "job_id", "job_status", "ci_state", "fix_count",
    "pr_number", "pr_url", "pr_draft", "pr",
}  # fmt: skip


def test_a_card_keeps_every_old_key_and_gains_plan_approval_and_started():
    plain = board._item_card(_item(), [], {})
    assert set(plain) == _OLD_CARD_KEYS | {"plan_approval", "started"}
    assert (plain["plan_approval"], plain["started"]) == (None, None)
    at = datetime.now(UTC)
    approved = _item(
        plan_section="moved-here",
        plan_approved_at=at,
        plan_approved_by="pryce",
        plan_approved_section="build",
        approved_plan_slug="pl",
        plan_approved_version=3,
        plan_changed=True,
        started_at=at,
        started_by="agent:orch",
    )
    full = board._item_card(approved, [], {})
    assert full["plan_approval"] == {
        "at": at,
        "by": "pryce",
        "plan": "pl",
        "version": 3,
        "section": "build",
        "changed_since": True,
    }
    assert full["started"] == {"at": at, "by": "agent:orch"}


def test_a_job_card_carries_the_new_keys_as_null():
    now = datetime.now(UTC)
    job = {
        "id": uuid.uuid4(),
        "status": "running",
        "title": "j",
        "requested_by": "console",
        "params": {},
        "meta": {},
        "created_at": now,
        "started_at": now,
        "finished_at": None,
    }
    found = board._job_card(job, {})
    assert set(found) == _OLD_CARD_KEYS | {"plan_approval", "started"}
    assert (found["plan_approval"], found["started"]) == (None, None)


async def test_a_card_read_gains_its_history(monkeypatch):
    history = [{"at": datetime.now(UTC), "kind": "plan_approved", "actor": "pryce", "detail": {}}]

    async def all_cards(all_done=False):
        return [board._item_card(_item(), [], {})]

    async def get_item(item_id):
        return {"summary": "", "why": "", "note": None}

    async def read_history(item_id):
        assert item_id == ITEM
        return history

    monkeypatch.setattr(board, "cards", all_cards)
    monkeypatch.setattr(board.backlog, "get_item", get_item)
    monkeypatch.setattr(board.events, "history", read_history)
    found = await board.card(f"item:{ITEM}")
    assert found["history"] == history


async def test_the_board_merges_each_item_plan_state_into_its_card(monkeypatch):
    async def items():
        return [_item()]

    async def empty():
        return {}

    async def none():
        return []

    async def no_sessions(hours):
        return []

    plan_id = uuid.uuid4()

    async def states():
        return {
            ITEM: {
                "plan_id": plan_id,
                "plan_approved_at": datetime.now(UTC),
                "plan_approved_by": "pryce",
                "plan_approved_version": 2,
                "started_at": None,
                "started_by": None,
                "plan_active": True,
                "plan_changed": False,
            }
        }

    monkeypatch.setattr(board.store, "items", items)
    monkeypatch.setattr(board.store, "chains", empty)
    monkeypatch.setattr(board.store, "pr_snapshots", empty)
    monkeypatch.setattr(board.store, "active_jobs", none)
    monkeypatch.setattr(board.events, "states", states)
    monkeypatch.setattr(board.sessions_store, "recent", no_sessions)
    read = await board.read()
    [found] = read.cards
    assert found["next"] == "Ready to start"
    assert found["plan_approval"]["by"] == "pryce"
    assert read.active_plans == {str(plan_id)}


def test_an_unknown_session_role_is_ignored_and_orchestrator_is_kept():
    base = {"session_id": "s", "ts": 1_763_600_000.5, "seq": 0, "kind": "start"}
    assert sessions.EventIn.model_validate(base | {"role": "orchestrator"}).role == "orchestrator"
    for junk in ("boss", "", 7, None, ["orchestrator"]):
        assert sessions.EventIn.model_validate(base | {"role": junk}).role is None
    [row] = sessions.reduce([sessions.EventIn.model_validate(base | {"role": "orchestrator"})])
    assert row["role"] == "orchestrator"
    [row] = sessions.reduce([sessions.EventIn.model_validate(base)])
    assert row["role"] is None


def test_a_session_view_carries_its_role():
    now = datetime.now(UTC)
    session = {
        "session_id": "s",
        "role": "orchestrator",
        "host": None,
        "cwd": None,
        "repo": None,
        "branch": None,
        "card_id": None,
        "started_at": now,
        "last_heard_at": now,
        "ended_at": None,
        "state": "idle",
        "doing": None,
        "doing_source": None,
        "doing_ts": None,
        "question": None,
        "question_ts": None,
        "question_seq": None,
        "cleared_ts": None,
        "cleared_seq": None,
    }
    link = {"status": "none", "card_id": None, "candidates": []}
    assert sessions.view(session, link, {}, {}, now)["role"] == "orchestrator"
    assert sessions.view(session | {"role": None}, link, {}, {}, now)["role"] is None


# S8: plans to approve


def _cmd_card(n, next_text, plan_id, kind="item"):
    return {"id": f"item:{n}", "kind": kind, "next": next_text, "plan_id": plan_id}


def test_plans_to_approve_holds_only_unapproved_cards_on_an_active_plan_in_board_order():
    cards = [
        _cmd_card(1, "Plan to approve", "active"),
        _cmd_card(2, "Plan to approve", "draft"),
        _cmd_card(3, "Plan to approve", None),
        _cmd_card(4, "Ready to start", "active"),
        _cmd_card(5, "Plan changed since approval", "active"),
        _cmd_card(6, "Plan to approve", "active"),
        _cmd_card(7, "Ready to start", None),
    ]
    parts = command.plan_parts(sessions.BoardRead(cards, [], {}, {"active"}))
    assert [c["id"] for c in parts["plans_to_approve"]] == ["item:1", "item:6"]
    assert parts["ready_to_start"] == 2


def test_the_board_order_puts_ranked_cards_first():
    now = datetime.now(UTC)
    ranked = {"rank": 2, "job_status": None, "updated_at": now}
    unranked = {"rank": None, "job_status": None, "updated_at": now}
    assert sorted([unranked, ranked], key=board._order)[0] is ranked


# The stale rule matches plans.get_plan


BODY = "# P\n\n## Build {#build}\n\nDo it.\n\n## Other {#other}\n\nElse.\n"


@pytest.mark.parametrize(
    "now, section, changed",
    [
        (BODY.replace("Else.", "Something else."), "build", False),
        (BODY.replace("Do it.", "Do it   differently."), "build", True),
        (BODY.replace("Do it.", "Do   it."), "build", False),
        (BODY.replace("## Build {#build}\n\nDo it.\n\n", ""), "build", False),
        (BODY.replace("Do it.", "Do more."), None, False),
        (BODY.replace("Do it.", "Do more."), "missing", False),
    ],
)
def test_an_approval_goes_stale_only_when_its_section_text_changed(now, section, changed):
    assert events.changed_since(BODY, now, section) is changed
