"""D7 stage 1: a dispatch plan submitted as one worker job that waits for approval. The plan rides
in the job's params; the worker key writes a plan's rows only while its dispatch job runs."""

import inspect
import json
import re
import uuid
from contextlib import asynccontextmanager

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app import policy
from app.board import dispatch
from app.config import settings
from app.dependencies import verify_api_key
from app.jobs import dispatch_jobs, service, store
from app.learning import service as learning
from app.migrations import _discover
from app.routers import dispatch as dispatch_router
from app.routers import jobs as jobs_router
from app.routers import worker

JOB_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
WORKER_KEY = "worker-key"
CONSOLE_KEY = "console-key"
CONSOLE = {"X-API-Key": CONSOLE_KEY}
WORKER = {"X-Worker-Key": WORKER_KEY}


async def test_create_job_inserts_the_same_row_through_the_pool(monkeypatch):
    seen = []

    class OnePool:
        async def fetchrow(self, sql, *args):
            seen.append((sql, args))
            return {"id": JOB_ID, "params": args[6]}

    monkeypatch.setattr(store, "get_pool", lambda: OnePool())
    job = await store.create_job(
        kind="code_task",
        agent="claude",
        model="sonnet",
        profile="change",
        title="t",
        prompt="p",
        params={"repo": "tharpep/sazed"},
        requested_by="console",
    )
    assert job == {"id": JOB_ID, "params": {"repo": "tharpep/sazed"}}
    [(sql, args)] = seen
    assert "INSERT INTO jobs" in sql
    assert args[:5] == ("code_task", "claude", "sonnet", "change", "t")
    assert args[5:8] == ("p", json.dumps({"repo": "tharpep/sazed"}), "queued")
    assert args[8:] == (None, "console", store.PRIORITY_PRYCE, None)


def test_the_plan_hash_matches_sazed_workers_literal():
    plan = {
        "id": "hash-pin",
        "tasks": [{"id": "a", "goal": "g", "done": "d", "owns": ["x.py"], "check": "true"}],
    }
    assert dispatch_jobs.plan_hash(plan, {}) == (
        "927a43a75f48debbf6b4815b47e4c543abc37e92239d4a4a595d80d4dca9ae2e"
    )


PLAN = {
    "id": "temps-v1",
    "project": "agent-stack",
    "check": "python -m unittest",
    "interface_files": {"api.md": "héllo"},
    "tasks": [
        {"id": "api", "goal": "Add it", "done": "it works", "owns": ["a.py"], "model": "opus"},
        {"id": "ui", "goal": "Draw it", "done": "", "depends_on": ["api"], "profile": "design"},
    ],
}


def _body(**overrides):
    return {
        "plan": PLAN,
        "config": {"profiles": {"design": {"budget_usd": 2.0}}},
        "repo": "sazed-worker",
        "base": "main",
        "budget_usd": 5.0,
        "task_caps": {"api": 1.5, "ui": 2.0},
        "timeout_s": 7200,
        "parallel": 1,
        "title": "feat: temps\nsecond line",
        "body": "why",
        "board_card": "item:abc",
    } | overrides


def _with_plan(**overrides):
    return _body(plan=PLAN | overrides)


class Db:
    """The one connection submit uses; jsonb mimics Postgres returning the params it stored."""

    def __init__(self):
        self.calls, self.open_job, self.jsonb = [], False, lambda text: text

    @asynccontextmanager
    async def acquire(self):
        yield self

    @asynccontextmanager
    async def transaction(self):
        self.calls.append(("begin", "", ()))
        yield
        self.calls.append(("commit", "", ()))

    async def execute(self, sql, *args):
        self.calls.append(("execute", sql, args))

    async def fetchval(self, sql, *args):
        self.calls.append(("fetchval", sql, args))
        return 1 if self.open_job else None

    async def fetchrow(self, sql, *args):
        self.calls.append(("fetchrow", sql, args))
        return {
            "id": JOB_ID,
            "kind": args[0],
            "params": self.jsonb(args[6]),
            "status": args[7],
        }

    @property
    def inserted(self):
        return [a for _, sql, a in self.calls if "INSERT INTO jobs" in sql]

    @property
    def linked(self):
        return [a for _, sql, a in self.calls if "INSERT INTO dispatch_plans" in sql]


@pytest.fixture
def db(monkeypatch):
    state = Db()

    async def repo_info(repo):
        return {"full_name": repo, "private": True}

    monkeypatch.setattr(settings, "worker_key", WORKER_KEY)
    monkeypatch.setattr(settings, "api_key", CONSOLE_KEY)
    monkeypatch.setattr(settings, "code_task_excluded_repos", ["tharpep/Trusted-Model-Registry"])
    monkeypatch.setattr(dispatch_jobs, "get_pool", lambda: state)
    monkeypatch.setattr(service, "repo_info", repo_info)
    return state


@pytest.fixture
def client():
    app = FastAPI()
    console = [Depends(verify_api_key)]
    app.include_router(worker.router, prefix="/worker")
    app.include_router(dispatch_router.router, prefix="/dispatch", dependencies=console)
    return TestClient(app)


def _submit(client, body=None, plan="temps-v1", headers=CONSOLE):
    return client.post(f"/dispatch/plans/{plan}/submit", json=body or _body(), headers=headers)


def test_a_submitted_plan_becomes_a_job_waiting_for_approval(db, client):
    resp = _submit(client)
    digest = dispatch_jobs.plan_hash(PLAN, _body()["config"])
    assert resp.status_code == 201, resp.text
    assert resp.json() == {
        "job_id": str(JOB_ID),
        "plan": "temps-v1",
        "plan_hash": digest,
        "status": "needs_approval",
    }
    [row] = db.inserted
    kind, agent, _model, profile, title, prompt, params, status, parent, requested_by = row[:10]
    assert (kind, agent, profile) == ("dispatch", "claude", "dispatch")
    assert (status, parent, requested_by) == ("needs_approval", None, "dispatch:temps-v1")
    assert title == prompt == "feat: temps second line"
    sent = _body()
    assert json.loads(params) == {
        "plan_id": "temps-v1",
        "plan": PLAN,
        "config": sent["config"],
        "plan_hash": digest,
        "repo": "tharpep/sazed-worker",
        "base": "main",
        "private": True,
        "budget_usd": 5.0,
        "task_caps": sent["task_caps"],
        "timeout_s": 7200,
        "parallel": 1,
        "title": sent["title"],
        "body": "why",
        "board_card": "item:abc",
    }


def test_submit_links_the_plan_row_in_the_same_transaction(db, client):
    _submit(client)
    [(plan, repo, base, tasks, project, board_plan, job_id)] = db.linked
    assert (plan, repo, base) == ("temps-v1", "tharpep/sazed-worker", "main")
    assert (project, board_plan, job_id) == ("agent-stack", None, JOB_ID)
    api = {"id": "api", "goal": "Add it", "owns": ["a.py"], "depends_on": []}
    ui = {"id": "ui", "goal": "Draw it", "owns": [], "depends_on": ["api"]}
    assert json.loads(tasks) == [
        api | {"profile": "implement", "model": "opus"},
        ui | {"profile": "design", "model": ""},
    ]
    kinds = [c[0] for c in db.calls]
    assert (kinds[0], kinds[-1]) == ("begin", "commit")
    _, sql, args = db.calls[1]
    assert "pg_advisory_xact_lock" in sql and args == ("dispatch:temps-v1",)


@pytest.mark.parametrize(
    ("body", "words"),
    [
        (_with_plan(id="other"), "must match the path"),
        (_with_plan(tasks=[]), "no tasks"),
        (_with_plan(tasks=None), "no tasks"),
        (_with_plan(tasks={"id": "api"}), "no tasks"),
        (_with_plan(tasks=["api"]), "its own id"),
        (_with_plan(tasks=[{"id": "api"}, {"id": "api"}]), "its own id"),
        (_with_plan(tasks=[{"goal": "no id"}]), "plan tasks"),
        (_body(task_caps={"api": 1.5}), "exactly the plan's tasks"),
        (_body(task_caps={"api": 1, "ui": 1, "x": 1}), "exactly the plan's tasks"),
        (_body(task_caps={"api": 1.5, "ui": 0}), "over 0"),
        (_body(task_caps={"api": 1.5, "ui": -1}), "over 0"),
        (_body(task_caps={"api": 3, "ui": 2.01}), "over the $5.00 budget"),
        (_body(budget_usd=0), "greater than 0"),
        (_body(budget_usd=-1), "greater than 0"),
        (_body(timeout_s=599), "greater than or equal to 600"),
        (_body(timeout_s=14401), "less than or equal to 14400"),
        (_body(parallel=0), "greater than or equal to 1"),
        (_body(parallel=5), "less than or equal to 4"),
        (_body(repo="tharpep/trusted-model-registry"), "excluded"),
        (_body(repo="not a repo"), "a repo name"),
        (_with_plan(project="p" * 61), "plan project"),
        (_with_plan(check="\ud800"), "valid UTF-8"),
    ],
)
def test_a_bad_submission_is_refused_and_creates_nothing(db, client, body, words):
    text = json.dumps(body).encode()
    resp = client.post("/dispatch/plans/temps-v1/submit", content=text, headers=CONSOLE)
    assert resp.status_code == 422, resp.text
    assert words in resp.text
    assert db.calls == []


@pytest.mark.parametrize("plan", ["a..b", "x.lock", "x.", "a b", "p" * 76])
def test_a_plan_id_that_cant_form_a_branch_is_refused(db, client, plan):
    resp = _submit(client, _with_plan(id=plan), plan)
    assert resp.status_code == 422 and "branch name" in resp.text
    assert db.calls == []


def test_policy_is_asked_about_the_repo(db, client, monkeypatch):
    asked = []

    def decide(action):
        asked.append(action)
        return policy.Decision("block", "nope")

    monkeypatch.setattr(policy, "decide", decide)
    resp = _submit(client)
    assert resp.status_code == 422 and resp.json()["detail"] == "nope"
    assert asked == [policy.StartJob("claude", "dispatch:temps-v1", repo="tharpep/sazed-worker")]


def test_caps_that_meet_the_budget_are_accepted(db, client):
    assert 1.1 + 2.2 > 3.3
    resp = _submit(client, _body(budget_usd=3.3, task_caps={"api": 1.1, "ui": 2.2}))
    assert resp.status_code == 201


def test_a_number_jsonb_rewrites_is_refused(db, client):
    """JSONB returns 1e16 as an integer, whose hash the worker would refuse after approval."""
    db.jsonb = lambda text: text.replace("1e+16", "10000000000000000")
    resp = _submit(client, _with_plan(big=1e16))
    assert resp.status_code == 422 and "stored exactly" in resp.text
    assert db.linked == [] and "commit" not in [c[0] for c in db.calls]


def test_a_plan_with_an_open_dispatch_job_is_a_conflict(db, client):
    db.open_job = True
    resp = _submit(client)
    assert resp.status_code == 409 and "already has a dispatch job" in resp.text
    assert db.inserted == [] and db.linked == []
    [(_, sql, args)] = [c for c in db.calls if c[0] == "fetchval"]
    assert "kind = 'dispatch'" in sql
    assert args == ("temps-v1", ["needs_approval", "queued", "running"])


def test_without_a_worker_nothing_is_submitted(db, client, monkeypatch):
    monkeypatch.setattr(settings, "worker_key", "")
    assert _submit(client).status_code == 422
    assert db.calls == []


def test_the_worker_key_cannot_submit(db, client):
    assert _submit(client, headers=WORKER).status_code == 401
    assert db.calls == []


def test_no_statement_changes_a_jobs_params():
    for module in (store, dispatch, dispatch_jobs, service):
        assert not re.search(r"\bSET\b[^;]*?\bparams\s*=", inspect.getsource(module))


ROW = {"plan": "temps-v1", "task": "api", "ts": 1.5, "state": "running", "line": "go"}
STEPS = {
    "plan": "temps-v1",
    "task": "api",
    "steps": [{"ts": 1.5, "seq": 0, "kind": "tool", "tool": "Edit", "text": "a.py"}],
}
TWINS = {
    "rows": ROW,
    "heartbeat": {"plan": "temps-v1", "task": "api"},
    "steps": STEPS,
    "plans": {
        "plan": "temps-v1",
        "repo": "tharpep/sazed-worker",
        "base": "main",
        "host": "ct110",
        "tasks": [{"id": "api", "goal": "Add it", "profile": "implement", "model": "opus"}],
        "project": "agent-stack",
    },
}


class Pool:
    """Rows written, and the one job the plan's row points at."""

    def __init__(self):
        self.written, self.asked, self.learned = [], [], []
        self.job = {"kind": "dispatch", "status": "running"}

    async def execute(self, sql, *args):
        self.written.append((sql, args))

    async def executemany(self, sql, rows):
        self.written.append((sql, rows))

    async def fetchrow(self, sql, *args):
        self.asked.append((sql, args))
        wanted = dict(re.findall(r"j\.(kind|status) = '(\w+)'", sql))
        if args != ("temps-v1",) or self.job != wanted:
            return None
        return {"params": "{}"}


@pytest.fixture
def pool(db, monkeypatch):
    state = Pool()

    async def record_dispatch(*args):
        state.learned.append(args)

    monkeypatch.setattr(dispatch, "get_pool", lambda: state)
    monkeypatch.setattr(learning, "record_dispatch", record_dispatch)
    return state


@pytest.mark.parametrize("route", TWINS)
def test_a_twin_writes_what_the_console_route_writes(pool, client, route):
    console = client.post(f"/dispatch/{route}", json=TWINS[route], headers=CONSOLE)
    by_console, learned = list(pool.written), list(pool.learned)
    pool.written.clear()
    pool.learned.clear()
    twin = client.post(f"/worker/dispatch/{route}", json=TWINS[route], headers=WORKER)
    assert (twin.status_code, twin.json()) == (console.status_code, console.json())
    assert twin.is_success and by_console
    assert pool.written == by_console
    assert pool.learned == learned
    assert len(learned) == (1 if route == "rows" else 0)


@pytest.mark.parametrize("route", TWINS)
@pytest.mark.parametrize(
    "job",
    [
        None,
        {"kind": "code_task", "status": "running"},
        {"kind": "dispatch", "status": "needs_approval"},
        {"kind": "dispatch", "status": "queued"},
        {"kind": "dispatch", "status": "cancelled"},
        {"kind": "dispatch", "status": "succeeded"},
    ],
)
def test_a_twin_refuses_a_plan_with_no_running_dispatch_job(pool, client, route, job):
    pool.job = job
    resp = client.post(f"/worker/dispatch/{route}", json=TWINS[route], headers=WORKER)
    assert resp.status_code == 403 and "temps-v1" in resp.json()["detail"]
    assert pool.written == [] and pool.learned == []


@pytest.mark.parametrize("route", TWINS)
def test_a_twin_refuses_another_plan(pool, client, route):
    body = TWINS[route] | {"plan": "someone-elses"}
    assert client.post(f"/worker/dispatch/{route}", json=body, headers=WORKER).status_code == 403
    assert pool.written == []


@pytest.mark.parametrize("route", TWINS)
def test_each_route_takes_only_its_own_key(pool, client, route):
    path = f"/dispatch/{route}"
    assert client.post(path, json=TWINS[route], headers=WORKER).status_code == 401
    twin = client.post(f"/worker{path}", json=TWINS[route], headers=CONSOLE)
    assert twin.status_code == 401
    assert pool.written == []


async def test_the_gate_reads_the_job_the_plan_row_points_at(pool):
    assert await dispatch.running_job_params("temps-v1") == {}
    [(sql, _)] = pool.asked
    assert "JOIN jobs j ON j.id = p.job_id" in sql and "p.plan = $1" in sql


def test_a_mirrored_plan_never_touches_job_id(pool, client):
    client.post("/worker/dispatch/plans", json=TWINS["plans"], headers=WORKER)
    client.post("/worker/dispatch/rows", json=ROW, headers=WORKER)
    assert pool.written and all("job_id" not in sql for sql, _ in pool.written)
    sources = [inspect.getsource(m) for m in (dispatch, dispatch_jobs, worker, dispatch_router)]
    assert sum(len(re.findall(r"\bjob_id = \$\d", s)) for s in sources) == 1


def test_a_dispatch_job_is_not_capped_and_claims_with_pryces_jobs():
    assert not policy.counts_toward_cap("dispatch:temps-v1")
    assert store.claim_priority("dispatch:temps-v1") == store.PRIORITY_PRYCE


def test_the_dispatch_label_in_notices_and_the_activity_view():
    assert service._SHORT_LABEL["dispatch"] == "Dispatch"
    newest = next(
        sql
        for _, _, path in reversed(_discover())
        if "FUNCTION activity_job_label(" in (sql := path.read_text("utf-8"))
    )
    assert "WHEN 'dispatch' THEN 'Dispatch'" in newest


def test_run_rows_differs_from_038_only_in_the_task_branchs_job_id():
    def view(prefix):
        sql = next(p for _, _, p in _discover() if p.name.startswith(prefix)).read_text("utf-8")
        return " ".join(sql.split("CREATE OR REPLACE VIEW run_rows AS", 1)[1].split())

    ours = view("040_")
    assert ours.count("dp.job_id::text AS job_id, to_timestamp(l.first_ts)") == 1
    restored = ours.replace("dp.job_id::text AS job_id", "NULL::text AS job_id").replace(
        " LEFT JOIN dispatch_plans dp ON dp.plan = l.plan", ""
    )
    assert restored == view("038_")


async def test_the_job_list_carries_params_for_a_dispatch_job_only(monkeypatch):
    class Rows:
        async def fetch(self, sql, *args):
            assert "CASE WHEN kind = 'dispatch' THEN params END AS params" in sql
            return [
                {"id": 1, "kind": "dispatch", "params": '{"plan_id": "temps-v1"}'},
                {"id": 2, "kind": "research", "params": None},
            ]

    monkeypatch.setattr(store, "get_pool", lambda: Rows())
    assert await store.list_jobs() == [
        {"id": 1, "kind": "dispatch", "params": {"plan_id": "temps-v1"}},
        {"id": 2, "kind": "research"},
    ]


def _job(**overrides):
    return {
        "id": JOB_ID,
        "kind": "dispatch",
        "agent": "claude",
        "profile": "dispatch",
        "status": "needs_approval",
        "params": {"plan_id": "temps-v1"},
        "meta": {},
    } | overrides


async def test_one_job_carries_its_kind_and_params(monkeypatch):
    async def get_job(job_id):
        return _job()

    async def no_events(job_id, limit=0):
        return []

    monkeypatch.setattr(store, "get_job", get_job)
    monkeypatch.setattr(store, "get_recent_events", no_events)
    job = await jobs_router.get_job(JOB_ID, events=False)
    assert job["kind"] == "dispatch" and job["params"] == {"plan_id": "temps-v1"}


async def test_a_dispatch_job_cannot_be_continued(monkeypatch):
    """A follow-up copies its parent's params and can start queued, so it would skip approval."""

    async def get_job(job_id):
        return _job(status="cancelled", agent_session_id="s")

    async def create_job(**kwargs):
        raise AssertionError("a follow-up was created")

    monkeypatch.setattr(store, "get_job", get_job)
    monkeypatch.setattr(store, "create_job", create_job)
    with pytest.raises(service.JobError, match="submit the plan again"):
        await service.create_follow_up(JOB_ID, "carry on", "console", approved=True)
