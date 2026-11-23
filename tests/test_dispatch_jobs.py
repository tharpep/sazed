"""D7 stage 1: a dispatch plan submitted as one worker job that waits for approval. The plan rides
in the job's params; the worker key writes a plan's rows only while its dispatch job runs."""

import inspect
import json
import re
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app import policy
from app.board import dispatch
from app.config import settings
from app.dependencies import verify_api_key
from app.jobs import ci_watch, dispatch_jobs, service, store
from app.learning import service as learning
from app.learning.classes import CLASSES, DISPATCH_STOPS, classify
from app.migrations import _discover
from app.routers import dispatch as dispatch_router
from app.routers import jobs as jobs_router
from app.routers import worker
from tests.test_dispatch_failure_class import MAPPING

JOB_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
WORKER_KEY = "worker-key"
CONSOLE_KEY = "console-key"
CONSOLE = {"X-API-Key": CONSOLE_KEY}
WORKER = {"X-Worker-Key": WORKER_KEY, "X-Dispatch-Job": str(JOB_ID)}


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
        self.stored, self.pr, self.pr_reads = None, None, []

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

    async def plan_row(plan):
        return state.stored

    async def pull_request(repo, number):
        state.pr_reads.append((repo, number))
        return state.pr

    monkeypatch.setattr(service, "repo_info", repo_info)
    monkeypatch.setattr(dispatch, "plan_row", plan_row)
    monkeypatch.setattr(ci_watch, "pull_request", pull_request)
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
        "title": "feat: temps second line",
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
        (_body(budget_usd=1e-7, task_caps={"api": 4e-7, "ui": 4e-7}), "over the"),
        (_body(budget_usd=5, task_caps={"api": 4.9999999, "ui": 2e-7}), "over the"),
        (_body(title="   "), "title is blank"),
        (_body(title="\n\t"), "title is blank"),
        (_with_plan(tasks=5), "no tasks"),
        (_with_plan(tasks=True), "no tasks"),
        (_with_plan(tasks="abc"), "no tasks"),
        (_with_plan(regen_outputs="gen/*"), "regen_outputs"),
        (_with_plan(interface_files=["a"]), "interface_files"),
        (_with_plan(project=7), "plan project"),
        ([], "valid dictionary"),
        (_body(plan=None), "valid dictionary"),
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


@pytest.mark.parametrize(
    ("raw", "words"),
    [
        (b"not json", "JSON"),
        (b"\xff\xfe", "JSON"),
        (b'{"budget_usd": Infinity}', "JSON"),
        (b'{"plan": {"x": NaN}}', "JSON"),
        (b'{"budget_usd": 1e400}', "plan: Field required"),
        (b'{"plan": ' + b'{"a": ' * 3000 + b"1" + b"}" * 3001, "deeper"),
        (b'{"plan": ' + b'{"a": ' * 100 + b"1" + b"}" * 101, "deeper"),
        (b'{"plan": ' + b"[" * 300 + b"]" * 300 + b"}", "deeper"),
        (b'{"plan": {"x": "' + b"y" * dispatch_jobs.MAX_BODY + b'"}}', "larger than 1 MiB"),
    ],
    ids=lambda v: repr(v)[:40],
)
def test_a_body_that_cant_be_read_is_refused(db, client, raw, words):
    resp = client.post("/dispatch/plans/temps-v1/submit", content=raw, headers=CONSOLE)
    assert resp.status_code == 422, resp.text
    assert words in resp.text
    assert db.calls == []


def test_a_body_at_the_size_limit_is_accepted(db, client):
    text = json.dumps(_with_plan(pad="")).encode()
    pad = "y" * (dispatch_jobs.MAX_BODY - len(text))
    text = json.dumps(_with_plan(pad=pad)).encode()
    assert len(text) == dispatch_jobs.MAX_BODY
    resp = client.post("/dispatch/plans/temps-v1/submit", content=text, headers=CONSOLE)
    assert resp.status_code == 201


def _stored(**overrides):
    return {
        "plan": "temps-v1",
        "repo": "tharpep/sazed-worker",
        "base": "main",
        "pr_number": 5,
        "pr_url": "https://github.com/tharpep/sazed-worker/pull/5",
    } | overrides


DRAFT = {"number": 5, "state": "open", "draft": True, "base": "main"}


@pytest.mark.parametrize(
    ("stored", "pr", "status"),
    [
        (_stored(pr_number=None, pr_url=None), None, 201),
        (_stored(), DRAFT, 201),
        (_stored(repo="mirrored/elsewhere", base="dev"), DRAFT, 201),
        (_stored(), DRAFT | {"draft": False}, 409),
        (_stored(), DRAFT | {"state": "closed"}, 409),
        (_stored(), DRAFT | {"state": "closed", "merged": True}, 409),
        (_stored(), DRAFT | {"base": "dev"}, 409),
        (_stored(pr_url="https://github.com/tharpep/sazed-worker-2/pull/5"), DRAFT, 409),
        (_stored(pr_url=None), DRAFT, 409),
        (_stored(), None, 503),
    ],
)
def test_a_resubmit_needs_a_stored_pr_it_can_update(db, client, stored, pr, status):
    db.stored, db.pr = stored, pr
    resp = _submit(client)
    assert resp.status_code == status, resp.text
    assert len(db.linked) == (1 if status == 201 else 0)
    assert all(read == ("tharpep/sazed-worker", 5) for read in db.pr_reads)


def test_an_unreachable_gateway_is_503(db, client, monkeypatch):
    async def repo_info(repo):
        raise service.GatewayUnreachable("couldn't look up the repo")

    monkeypatch.setattr(service, "repo_info", repo_info)
    assert _submit(client).status_code == 503
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
    resp = _submit(client, _body(budget_usd=1e-7, task_caps={"api": 5e-8, "ui": 5e-8}))
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


APPROVED = {
    "repo": "tharpep/sazed-worker",
    "base": "main",
    "plan": PLAN | {"board_plan": "p17"},
}


class Pool:
    """Rows written, and the plan's current job as approved_params' SQL may find it."""

    def __init__(self):
        self.written, self.asked, self.learned = [], [], []
        self.job = JOB_ID

    async def execute(self, sql, *args):
        self.written.append((sql, args))

    async def executemany(self, sql, rows):
        self.written.append((sql, rows))

    async def fetchrow(self, sql, *args):
        self.asked.append((sql, args))
        if args[:2] != ("temps-v1", self.job):
            return None
        return {"params": json.dumps(APPROVED)}


@pytest.fixture
def pool(db, monkeypatch):
    state = Pool()

    async def record_dispatch(*args):
        state.learned.append(args)

    monkeypatch.setattr(dispatch, "get_pool", lambda: state)
    monkeypatch.setattr(learning, "record_dispatch", record_dispatch)
    return state


def _twin(client, route, body=None, headers=WORKER):
    return client.post(f"/worker/dispatch/{route}", json=body or TWINS[route], headers=headers)


@pytest.mark.parametrize("route", TWINS)
def test_a_twin_writes_what_the_console_route_writes(pool, client, route):
    body = TWINS[route] | ({"board_plan": "p17"} if route == "plans" else {})
    console = client.post(f"/dispatch/{route}", json=body, headers=CONSOLE)
    by_console, learned = list(pool.written), list(pool.learned)
    pool.written.clear()
    pool.learned.clear()
    twin = _twin(client, route, body)
    assert (twin.status_code, twin.json()) == (console.status_code, console.json())
    assert twin.is_success and by_console
    assert pool.written == by_console
    assert pool.learned == learned
    assert len(learned) == (1 if route == "rows" else 0)


@pytest.mark.parametrize("route", TWINS)
@pytest.mark.parametrize("job", [None, "", "not-a-uuid", "33333333-3333-3333-3333-333333333333"])
def test_a_twin_refuses_any_job_but_the_plans_current_one(pool, client, route, job):
    headers = {"X-Worker-Key": WORKER_KEY} | ({} if job is None else {"X-Dispatch-Job": job})
    resp = _twin(client, route, headers=headers)
    assert resp.status_code == 403 and "temps-v1" in resp.json()["detail"]
    assert pool.written == [] and pool.learned == []


@pytest.mark.parametrize("route", TWINS)
def test_a_twin_refuses_another_plan(pool, client, route):
    assert _twin(client, route, TWINS[route] | {"plan": "someone-elses"}).status_code == 403
    assert pool.written == []


@pytest.mark.parametrize("route", TWINS)
def test_a_twin_refuses_a_task_the_plan_does_not_have(pool, client, route):
    body = dict(TWINS[route])
    if route == "plans":
        body["tasks"] = [*body["tasks"], {"id": "extra"}]
    else:
        body["task"] = "extra"
    resp = _twin(client, route, body)
    assert resp.status_code == 403 and "no task extra" in resp.json()["detail"]
    assert pool.written == []


def test_a_mirrored_plan_keeps_what_was_approved(pool, client):
    moved = {"repo": "tharpep/other", "base": "dev", "project": "x", "board_plan": "y"}
    assert _twin(client, "plans", TWINS["plans"] | moved).is_success
    [(_, args)] = pool.written
    plan, repo, base, host, _tasks, project, board_plan = args
    assert (plan, repo, base, host) == ("temps-v1", "tharpep/sazed-worker", "main", "ct110")
    assert (project, board_plan) == ("agent-stack", "p17")


@pytest.mark.parametrize("state", ["running", "done", "waiting", "landed", "conflict", "cleaned"])
def test_only_an_ending_row_is_a_last_word(pool, client, state):
    assert _twin(client, "rows", ROW | {"state": state}).is_success
    assert pool.asked[-1][1][3] is False


@pytest.mark.parametrize("state", ["failed", "timed_out", "blocked"])
def test_an_ending_row_may_be_a_last_word(pool, client, state):
    assert _twin(client, "rows", ROW | {"state": state}).is_success
    assert pool.asked[-1][1][3] is True


@pytest.mark.parametrize("route", ["heartbeat", "steps", "plans"])
def test_the_other_twins_close_at_once(pool, client, route):
    _twin(client, route)
    assert pool.asked[-1][1][3] is False


@pytest.mark.parametrize("route", TWINS)
def test_each_route_takes_only_its_own_key(pool, client, route):
    path = f"/dispatch/{route}"
    assert client.post(path, json=TWINS[route], headers=WORKER).status_code == 401
    twin = client.post(f"/worker{path}", json=TWINS[route], headers=CONSOLE)
    assert twin.status_code == 401
    assert pool.written == []


async def test_the_gate_reads_the_current_job_inside_its_wall_clock(pool, monkeypatch):
    monkeypatch.setattr(settings, "job_heartbeat_grace_s", 120)
    assert await dispatch.approved_params("temps-v1", str(JOB_ID), True) == APPROVED
    [(sql, args)] = pool.asked
    flat = " ".join(sql.split())
    assert "JOIN jobs j ON j.id = p.job_id" in flat
    assert "WHERE p.plan = $1 AND j.id = $2 AND j.kind = 'dispatch'" in flat
    assert "j.status = 'running' AND NOW() <= j.started_at + make_interval(" in flat
    assert args == ("temps-v1", JOB_ID, 120, True, 300)


NOT_A_TIME = ["1e18", "4e9", "Infinity", "-Infinity", "NaN", "-1", "1e400"]


@pytest.mark.parametrize("prefix", ["/dispatch", "/worker/dispatch"])
@pytest.mark.parametrize("ts", NOT_A_TIME)
def test_a_row_or_step_outside_the_clock_is_refused(pool, client, prefix, ts):
    headers = CONSOLE | WORKER
    row = json.dumps(ROW).replace("1.5", ts)
    steps = json.dumps(STEPS).replace("1.5", ts)
    for route, text in (("rows", row), ("steps", steps)):
        resp = client.post(f"{prefix}/{route}", content=text, headers=headers)
        assert resp.status_code == 422, resp.text
    assert pool.written == [] and pool.learned == []


@pytest.mark.parametrize("prefix", ["/dispatch", "/worker/dispatch"])
@pytest.mark.parametrize("cost", ["NaN", "Infinity", "-0.01", "1e400"])
def test_a_cost_that_is_not_an_amount_is_refused(pool, client, prefix, cost):
    text = json.dumps(ROW | {"cost_usd": 1234.5}).replace("1234.5", cost)
    resp = client.post(f"{prefix}/rows", content=text, headers=CONSOLE | WORKER)
    assert resp.status_code == 422, resp.text
    assert pool.written == [] and pool.learned == []


@pytest.mark.parametrize("seq", [2**31, -(2**31) - 1])
def test_a_seq_outside_int4_is_refused(pool, client, seq):
    body = STEPS | {"steps": [STEPS["steps"][0] | {"seq": seq}]}
    resp = client.post("/dispatch/steps", json=body, headers=CONSOLE)
    assert resp.status_code == 422 and pool.written == []


def test_a_clock_a_little_ahead_is_accepted(pool, client):
    soon = ROW | {"ts": time.time() + 3000, "cost_usd": 0}
    assert client.post("/dispatch/rows", json=soon, headers=CONSOLE).status_code == 201


def test_a_mirrored_plan_never_touches_job_id(pool, client):
    _twin(client, "plans")
    _twin(client, "rows")
    assert pool.written and all("job_id" not in sql for sql, _ in pool.written)
    sources = [inspect.getsource(m) for m in (dispatch, dispatch_jobs, worker, dispatch_router)]
    assert sum(len(re.findall(r"\bjob_id = \$\d", src)) for src in sources) == 1


VECTORS = json.loads(
    (Path(__file__).parent / "data" / "owns_glob_vectors.json").read_text(encoding="utf-8")
)


@pytest.mark.parametrize(("path", "pattern", "expected"), VECTORS)
def test_owns_globs_match_like_sazed_workers(path, pattern, expected):
    assert dispatch_jobs.glob_match(path, pattern) is expected


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


async def test_the_job_list_carries_no_params(monkeypatch):
    seen = []

    class Rows:
        async def fetch(self, sql, *args):
            seen.append(sql)
            return [{"id": 1, "kind": "dispatch"}]

    monkeypatch.setattr(store, "get_pool", lambda: Rows())
    assert await store.list_jobs() == [{"id": 1, "kind": "dispatch"}]
    assert "params" not in seen[0]


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


@pytest.mark.parametrize(("sent", "kinds"), [({}, []), ({"kinds": ["dispatch"]}, ["dispatch"])])
def test_a_claim_passes_the_kinds_the_worker_runs(db, client, monkeypatch, sent, kinds):
    asked = []

    async def claim_next(agents, kinds):
        asked.append((agents, kinds))

    async def expire_stale():
        return []

    monkeypatch.setattr(store, "claim_next", claim_next)
    monkeypatch.setattr(store, "expire_stale", expire_stale)
    resp = client.post("/worker/claim", json={"agents": ["claude"]} | sent, headers=WORKER)
    assert resp.status_code == 204
    assert asked == [(["claude"], kinds)]


async def test_only_a_worker_that_lists_dispatch_claims_a_dispatch_job(monkeypatch):
    seen = []

    class OnePool:
        async def fetchrow(self, sql, *args):
            seen.append((sql, args))

    monkeypatch.setattr(store, "get_pool", lambda: OnePool())
    await store.claim_next(["claude"])
    await store.claim_next(["claude"], ["dispatch"])
    assert "AND (kind <> 'dispatch' OR kind = ANY($2::text[]))" in seen[0][0]
    assert [args for _, args in seen] == [(["claude"], []), (["claude"], ["dispatch"])]


@pytest.mark.parametrize(
    ("kind", "status"), [("dispatch", "succeeded"), ("research", "needs_input")]
)
def test_a_dispatch_job_never_asks(db, client, monkeypatch, kind, status):
    finished = []

    async def get_job(job_id):
        return _job(kind=kind, profile=kind, status="running")

    async def finish_job(job_id, status, result, error, session, meta):
        finished.append((status, meta))

    async def keep_session(job_id, session):
        pass

    monkeypatch.setattr(store, "get_job", get_job)
    monkeypatch.setattr(store, "finish_job", finish_job)
    monkeypatch.setattr(store, "keep_session", keep_session)
    body = {"status": "succeeded", "result": "done\nNEEDS_INPUT: send your key?"}
    assert client.post(f"/worker/jobs/{JOB_ID}/finish", json=body, headers=WORKER).is_success
    [(got, meta)] = finished
    assert got == status
    assert ("question" in meta) is (status == "needs_input")


def test_a_dispatch_jobs_stops_are_dispatch_failure_classs_stops():
    stops = {r: c for r, c in MAPPING.items() if c and CLASSES[c].family == "stop"}
    assert DISPATCH_STOPS == stops | {"no_heartbeat": "worker_lost"}


@pytest.mark.parametrize("status", ["failed", "timed_out", "cancelled"])
@pytest.mark.parametrize(
    ("reason", "cls"),
    [
        ("timeout", "timed_out"),
        ("plan_limit", "rate_limit"),
        ("budget", "budget"),
        ("cancelled", "cancelled"),
        ("no_result", None),
        ("check_failed", None),
        ("conflict", None),
        ("refused", None),
        (None, None),
        (["timeout"], None),
    ],
)
def test_a_dispatch_job_never_counts_as_a_lesson(status, reason, cls):
    meta = {} if reason is None else {"reason": reason, "stop_reason": "tool_cap"}
    job = _job(status=status, error="usage limit: worker error", meta=meta)
    assert classify(job) == cls


def test_a_dispatch_job_that_succeeded_has_no_class():
    assert classify(_job(status="succeeded", meta={"reason": "budget"})) is None
