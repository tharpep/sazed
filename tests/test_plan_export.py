"""POST /worker/dispatch/plans/{plan}/export (One stack seam 6): the worker's plan export opens or
updates one draft PR through the gateway, and every refusal carries a reason code."""

import httpx
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.board import dispatch
from app.config import settings
from app.dependencies import verify_api_key
from app.jobs import ci_watch, service
from app.routers import dispatch as dispatch_router
from app.routers import worker

WORKER_KEY = "worker-key"
CONSOLE_KEY = "console-key"
BASE_SHA = "a" * 40
PLAN_HASH = "c" * 64
JOB_ID = "22222222-2222-2222-2222-222222222222"
URL = "/worker/dispatch/plans/temps-v1/export"


def _plan(**overrides):
    return {
        "plan": "temps-v1",
        "repo": "tharpep/dispatch-sandbox",
        "base": "main",
        "host": "ct110",
        "tasks": [],
        "pr_url": None,
        "pr_number": None,
        "pr_head_sha": None,
        "exported_at": None,
    } | overrides


def _body(**overrides):
    return {
        "plan_hash": PLAN_HASH,
        "repo": "tharpep/dispatch-sandbox",
        "base": "main",
        "base_sha": BASE_SHA,
        "title": "Dispatch plan temps-v1",
        "body": "- api: add the endpoint",
        "files": {"a.py": "eA==", "gone.py": None},
        "modes": {"run.sh": "100755"},
    } | overrides


class _Resp:
    def __init__(self, status, payload):
        self.status_code, self.payload = status, payload
        self.is_success = status < 400
        self.text = str(payload)

    def json(self):
        return self.payload

    def raise_for_status(self):
        if not self.is_success:
            raise httpx.HTTPStatusError("error", request=None, response=None)


class Gateway:
    def __init__(self):
        self.posts, self.looked_up, self.recorded = [], [], []
        self.repo_status = 200
        self.running, self.asked = True, []
        self.plan = {"tasks": [{"id": "api", "owns": ["**"]}]}
        self.pr = _Resp(
            201,
            {
                "url": "https://github.com/tharpep/dispatch-sandbox/pull/3",
                "number": 3,
                "branch": "sazed/plan-temps-v1",
                "updated": False,
                "head_sha": "b" * 40,
                "draft": True,
                "base_moved": False,
            },
        )
        self.raise_on_post = None
        self.raise_on_get = None
        self.pr_read = _Resp(200, {"number": 3, "state": "open", "draft": True})

    async def get(self, url, headers, timeout):
        if self.raise_on_get:
            raise self.raise_on_get
        self.looked_up.append(url)
        if "/pulls/" in url:
            return self.pr_read
        return _Resp(self.repo_status, {"full_name": url.rsplit("/repos/", 1)[1]})

    async def post(self, url, json, headers, timeout):
        if self.raise_on_post:
            raise self.raise_on_post
        self.posts.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        return self.pr


@pytest.fixture
def gateway(monkeypatch):
    gw = Gateway()
    plans = {"temps-v1": _plan()}

    async def plan_row(plan):
        return plans.get(plan)

    async def approved_params(plan, job_id, last_words=False):
        gw.asked.append((plan, job_id, last_words))
        row = plans.get(plan)
        if row is None or job_id != JOB_ID or not gw.running:
            return None
        return {
            "plan_hash": PLAN_HASH,
            "repo": service.full_repo(row["repo"]),
            "base": row["base"],
            "title": "Dispatch plan temps-v1",
            "plan": gw.plan,
        }

    async def record_export(plan, pr_url, pr_number, head_sha):
        gw.recorded.append((plan, pr_url, pr_number, head_sha))

    monkeypatch.setattr(settings, "worker_key", WORKER_KEY)
    monkeypatch.setattr(settings, "api_key", CONSOLE_KEY)
    monkeypatch.setattr(settings, "gateway_url", "http://gw")
    monkeypatch.setattr(settings, "gateway_api_key", "gw-key")
    monkeypatch.setattr(service, "get_client", lambda: gw)
    monkeypatch.setattr(ci_watch, "get_client", lambda: gw)
    monkeypatch.setattr(settings, "code_task_excluded_repos", ["tharpep/Trusted-Model-Registry"])
    monkeypatch.setattr(dispatch, "plan_row", plan_row)
    monkeypatch.setattr(dispatch, "approved_params", approved_params)
    monkeypatch.setattr(dispatch, "record_export", record_export)
    gw.plans = plans
    return gw


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(worker.router, prefix="/worker")
    app.include_router(
        dispatch_router.router, prefix="/dispatch", dependencies=[Depends(verify_api_key)]
    )
    return TestClient(app)


def _export(client, body=None, url=URL, key=WORKER_KEY, job=JOB_ID):
    headers = {"X-Worker-Key": key, "X-Dispatch-Job": job}
    return client.post(url, json=body or _body(), headers=headers)


def test_the_worker_key_is_required_and_the_console_key_is_refused(gateway, client):
    assert _export(client).status_code == 200
    console = client.post(URL, json=_body(), headers={"X-API-Key": CONSOLE_KEY})
    assert console.status_code == 401
    assert _export(client, key=CONSOLE_KEY).status_code == 401


def test_without_a_worker_key_configured_the_route_is_disabled(gateway, client, monkeypatch):
    monkeypatch.setattr(settings, "worker_key", "")
    assert _export(client).status_code == 503


def test_a_first_export_opens_one_draft_pr_on_the_plan_branch(gateway, client):
    resp = _export(client)
    assert resp.status_code == 200
    assert resp.json() == {
        "url": "https://github.com/tharpep/dispatch-sandbox/pull/3",
        "number": 3,
        "branch": "sazed/plan-temps-v1",
        "head_sha": "b" * 40,
        "updated": False,
        "base_moved": False,
    }
    assert gateway.posts == [
        {
            "url": "http://gw/github/app/pull-request",
            "json": {
                "repo": "tharpep/dispatch-sandbox",
                "branch": "sazed/plan-temps-v1",
                "title": "Dispatch plan temps-v1",
                "body": "- api: add the endpoint",
                "commit_message": "Dispatch plan temps-v1\n\nSazed dispatch plan temps-v1",
                "files": {"a.py": "eA==", "gone.py": None},
                "draft": True,
                "pr_number": None,
                "base": "main",
                "base_sha": BASE_SHA,
                "modes": {"run.sh": "100755"},
            },
            "headers": {"X-API-Key": "gw-key"},
            "timeout": 120.0,
        }
    ]
    assert gateway.recorded == [
        ("temps-v1", "https://github.com/tharpep/dispatch-sandbox/pull/3", 3, "b" * 40)
    ]


def test_a_re_export_updates_the_plans_pr_and_reports_a_moved_base(gateway, client):
    gateway.plans["temps-v1"] = _plan(pr_number=3)
    gateway.pr.payload |= {"updated": True, "base_moved": True}
    resp = _export(client)
    assert resp.status_code == 200
    assert (resp.json()["updated"], resp.json()["base_moved"]) == (True, True)
    assert gateway.posts[0]["json"]["pr_number"] == 3


def test_a_bare_repo_name_is_resolved_with_the_default_owner(gateway, client):
    gateway.plans["temps-v1"] = _plan(repo="obscura")
    resp = _export(client, _body(repo="obscura"))
    assert resp.status_code == 200
    assert gateway.looked_up == ["http://gw/github/repos/tharpep/obscura"]
    assert gateway.posts[0]["json"]["repo"] == "tharpep/obscura"


def _refusal(resp, status, reason):
    assert resp.status_code == status, resp.text
    assert resp.json()["reason"] == reason
    assert isinstance(resp.json()["detail"], str) and resp.json()["detail"]
    return resp.json()["detail"]


def test_a_repo_that_is_not_found_is_refused(gateway, client):
    gateway.repo_status = 404
    detail = _refusal(_export(client), 422, "refused")
    assert "not found" in detail
    assert gateway.posts == [] and gateway.recorded == []


def test_an_unknown_plan_is_refused(gateway, client):
    url = "/worker/dispatch/plans/nope/export"
    detail = _refusal(_export(client, url=url), 404, "refused")
    assert "nope" in detail
    assert gateway.posts == []


@pytest.mark.parametrize("plan", ["a..b", "x.lock", "x.", "a b", "é", "p" * 76])
def test_a_plan_id_that_cant_form_a_branch_is_refused(gateway, client, plan):
    url = f"/worker/dispatch/plans/{plan}/export"
    _refusal(_export(client, url=url), 422, "refused")
    assert gateway.posts == []


def test_the_longest_plan_id_fits_the_gateways_branch_pattern(gateway, client):
    gateway.plans["p" * 75] = _plan(plan="p" * 75)
    assert _export(client, url=f"/worker/dispatch/plans/{'p' * 75}/export").status_code == 200
    assert len(gateway.posts[0]["json"]["branch"].removeprefix("sazed/")) == 80


def test_a_body_for_another_repo_or_base_is_refused(gateway, client):
    _refusal(_export(client, _body(repo="tharpep/sazed")), 422, "refused")
    _refusal(_export(client, _body(base="dev")), 422, "refused")
    assert gateway.posts == []


def test_a_malformed_body_is_refused_with_a_reason(gateway, client):
    detail = _refusal(_export(client, _body(base_sha="main")), 422, "refused")
    assert detail.startswith("base_sha")
    resp = client.post(URL, content=b"not json", headers={"X-Worker-Key": WORKER_KEY})
    _refusal(resp, 422, "refused")


def test_an_export_over_the_gateways_limits_is_too_large(gateway, client):
    many = {f"f{i}.py": "eA==" for i in range(201)}
    _refusal(_export(client, _body(files=many)), 413, "export_too_large")
    big = {"big.bin": "A" * (5 * 1024 * 1024 * 4 // 3 + 1)}
    _refusal(_export(client, _body(files=big)), 413, "export_too_large")
    assert gateway.posts == []


@pytest.mark.parametrize(
    "error", [httpx.ReadTimeout("slow"), httpx.ConnectError("refused")], ids=["timeout", "down"]
)
def test_an_unreachable_gateway_is_export_unreachable(gateway, client, error):
    gateway.raise_on_post = error
    _refusal(_export(client), 424, "export_unreachable")
    gateway.raise_on_post, gateway.raise_on_get = None, error
    _refusal(_export(client), 424, "export_unreachable")
    assert gateway.recorded == []


def test_github_failing_behind_the_gateway_is_export_unreachable(gateway, client):
    gateway.pr = _Resp(502, {"detail": "GitHub unreachable: timed out"})
    detail = _refusal(_export(client), 424, "export_unreachable")
    assert detail == "GitHub unreachable: timed out"


def test_a_closed_pr_on_the_branch_is_pr_closed(gateway, client):
    gateway.plans["temps-v1"] = _plan(pr_number=3)
    sentence = "PR #3 is closed or no longer on sazed/plan-temps-v1; not reopening it"
    gateway.pr_read = _Resp(200, {"number": 3, "state": "closed", "draft": False})
    gateway.pr = _Resp(409, {"detail": sentence})
    assert _refusal(_export(client), 409, "pr_closed") == sentence
    assert gateway.recorded == []


def test_a_branch_whose_history_moved_is_refused_not_pr_closed(gateway, client):
    gateway.plans["temps-v1"] = _plan(pr_number=3)
    sentence = "sazed/plan-temps-v1 no longer grows from base_sha; re-export from a fresh base"
    gateway.pr = _Resp(409, {"detail": sentence})
    assert _refusal(_export(client), 409, "refused") == sentence


def test_a_re_export_of_a_draft_reads_it_then_updates_it(gateway, client):
    gateway.plans["temps-v1"] = _plan(pr_number=3)
    assert _export(client).status_code == 200
    assert "http://gw/github/repos/tharpep/dispatch-sandbox/pulls/3" in gateway.looked_up
    assert len(gateway.posts) == 1


def test_a_pr_marked_ready_is_not_rewritten(gateway, client):
    gateway.plans["temps-v1"] = _plan(pr_number=3)
    gateway.pr_read = _Resp(200, {"number": 3, "state": "open", "draft": False})
    detail = _refusal(_export(client), 409, "refused")
    assert detail == "PR #3 was marked ready, so export won't rewrite it"
    assert gateway.posts == [] and gateway.recorded == []


def test_a_pr_that_cant_be_read_is_export_unreachable(gateway, client):
    gateway.plans["temps-v1"] = _plan(pr_number=3)
    gateway.pr_read = _Resp(502, {"detail": "GitHub unreachable"})
    _refusal(_export(client), 424, "export_unreachable")
    assert gateway.posts == []


def test_a_repo_excluded_from_code_tasks_is_refused(gateway, client):
    gateway.plans["temps-v1"] = _plan(repo="Trusted-Model-Registry")
    detail = _refusal(_export(client, _body(repo="Trusted-Model-Registry")), 422, "refused")
    assert "excluded" in detail
    assert gateway.posts == [] and gateway.looked_up == []


def test_a_mode_the_gateway_does_not_take_is_refused(gateway, client):
    detail = _refusal(_export(client, _body(modes={"sub": "160000"})), 422, "refused")
    assert detail.startswith("modes.sub:")
    assert gateway.posts == []


def test_modes_reach_the_gateway_unchanged(gateway, client):
    modes = {"run.sh": "100755", "link": "120000", "was-exec.sh": "100644"}
    assert _export(client, _body(modes=modes)).status_code == 200
    assert gateway.posts[0]["json"]["modes"] == modes


def test_any_other_gateway_refusal_is_refused_with_its_sentence(gateway, client):
    for sentence in (
        "changes to '.github/workflows/ci.yml' aren't allowed",
        f"base_sha {BASE_SHA} isn't an ancestor of 'main'",
        "mode for 'gone.py', which has no content in files",
    ):
        gateway.pr = _Resp(422, {"detail": sentence})
        assert _refusal(_export(client), 422, "refused") == sentence


def test_a_plan_with_no_running_dispatch_job_is_refused(gateway, client):
    gateway.running = False
    assert "no running dispatch job" in _refusal(_export(client), 403, "refused")
    assert gateway.posts == [] and gateway.recorded == []


def test_another_plan_hash_is_refused(gateway, client):
    detail = _refusal(_export(client, _body(plan_hash="d" * 64)), 403, "refused")
    assert "isn't the plan that was approved" in detail
    assert gateway.posts == [] and gateway.recorded == []


def test_the_export_names_its_job(gateway, client):
    _refusal(_export(client, job="33333333-3333-3333-3333-333333333333"), 403, "refused")
    resp = client.post(URL, json=_body(), headers={"X-Worker-Key": WORKER_KEY})
    _refusal(resp, 403, "refused")
    assert gateway.posts == []
    _export(client)
    assert gateway.asked[-1] == ("temps-v1", JOB_ID, False)


def test_a_job_cancelled_while_the_gateway_answered_opens_nothing(gateway, client, monkeypatch):
    async def repo_info(repo):
        gateway.running = False
        return {}

    monkeypatch.setattr(service, "repo_info", repo_info)
    _refusal(_export(client), 403, "refused")
    assert gateway.posts == [] and gateway.recorded == []


def test_the_pr_title_is_the_approved_one(gateway, client):
    assert _export(client, _body(title="Something else")).status_code == 200
    assert gateway.posts[0]["json"]["title"] == "Dispatch plan temps-v1"
    body = _body()
    del body["title"]
    assert _export(client, body).status_code == 200


def test_a_file_outside_the_approved_plan_is_left_owns(gateway, client):
    gateway.plan = {
        "tasks": [{"id": "api", "owns": ["app/**/*.py"]}, {"id": "ui", "owns": ["ui.ts"]}],
        "regen_outputs": ["gen/*.json"],
        "interface_files": {"docs/[api].md": "x", "docs/*.txt": "y"},
    }
    inside = ["app/a.py", "app/x/b.py", "ui.ts", "gen/a.json", "docs/[api].md"]
    assert _export(client, _body(files=dict.fromkeys(inside, "eA=="))).status_code == 200
    assert len(gateway.posts) == 1
    outside = ["app/a.ts", "gen/x/a.json", "docs/a.txt", "app/../b.py", "/app/a.py", "app//b.py"]
    for path in outside:
        detail = _refusal(_export(client, _body(files={path: None})), 403, "left_owns")
        assert path in detail
    assert len(gateway.posts) == 1


def test_a_missing_plan_hash_is_refused(gateway, client):
    body = _body()
    del body["plan_hash"]
    assert _refusal(_export(client, body), 422, "refused").startswith("plan_hash")
    assert gateway.posts == []


@pytest.mark.parametrize("moved", [{"repo": "tharpep/sazed"}, {"base": "dev"}])
def test_a_row_the_mirror_moved_is_not_exported(gateway, client, monkeypatch, moved):
    """The worker's mirror can rewrite the plan row; the approved job's repo and base still win."""

    async def approved_params(plan, job_id, last_words=False):
        return {"plan_hash": PLAN_HASH, "repo": "tharpep/dispatch-sandbox", "base": "main"}

    monkeypatch.setattr(dispatch, "approved_params", approved_params)
    gateway.plans["temps-v1"] = _plan(**moved)
    detail = _refusal(_export(client, _body(**moved)), 403, "refused")
    assert "approved for tharpep/dispatch-sandbox at main" in detail
    assert gateway.posts == []


async def test_the_new_shape_carries_the_plans_job(monkeypatch):
    job = {"job_id": "job-1", "job_status": "queued", "job_created_at": "2026-11-22T09:00:00Z"}
    exported = _plan(updated_at="2026-11-19T12:00:00+00:00") | job

    async def plan_row(plan):
        return exported

    async def nothing(*args):
        return []

    async def no_steps(starts):
        return {}

    async def plan_rows():
        return [exported, _plan(updated_at=exported["updated_at"])]

    monkeypatch.setattr(dispatch, "plan_row", plan_row)
    monkeypatch.setattr(dispatch, "plan_rows", plan_rows)
    monkeypatch.setattr(dispatch, "rows_for", nothing)
    monkeypatch.setattr(dispatch, "steps_since", no_steps)
    found = await dispatch_router.get_plan("temps-v1", v2=True)
    assert {k: found[k] for k in job} == job
    listed, by_hand = await dispatch_router.list_plans(v2=True)
    assert {k: listed[k] for k in job} == job
    assert {k: by_hand[k] for k in job} == dict.fromkeys(job)
    assert not set(job) & set(await dispatch_router.get_plan("temps-v1", v2=False))


async def test_the_new_shape_carries_the_plans_pr(monkeypatch):
    exported = _plan(
        pr_url="https://github.com/tharpep/dispatch-sandbox/pull/3",
        pr_number=3,
        pr_head_sha="b" * 40,
        exported_at="2026-11-19T12:00:00+00:00",
        updated_at="2026-11-19T12:00:00+00:00",
    )

    async def plan_row(plan):
        return exported

    async def plan_rows():
        return [exported]

    async def rows_for(plan=None):
        return []

    async def steps_since(starts):
        return {}

    monkeypatch.setattr(dispatch, "plan_row", plan_row)
    monkeypatch.setattr(dispatch, "plan_rows", plan_rows)
    monkeypatch.setattr(dispatch, "rows_for", rows_for)
    monkeypatch.setattr(dispatch, "steps_since", steps_since)

    found = await dispatch_router.get_plan("temps-v1", v2=True)
    assert {k: found[k] for k in dispatch.PR_FIELDS} == {
        "pr_url": "https://github.com/tharpep/dispatch-sandbox/pull/3",
        "pr_number": 3,
        "pr_head_sha": "b" * 40,
        "exported_at": "2026-11-19T12:00:00+00:00",
    }
    [listed] = await dispatch_router.list_plans(v2=True)
    assert listed["pr_number"] == 3
    old = await dispatch_router.get_plan("temps-v1", v2=False)
    assert not set(dispatch.PR_FIELDS) & set(old)
