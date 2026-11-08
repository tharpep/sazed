"""Agent profiles: validation, versioned saves, protected deletes, dispatch links to the board."""

from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.board import dispatch, profiles, projects
from app.routers import agent_profiles, board
from app.routers import dispatch as dispatch_router

NOW = datetime.now(UTC)
BODY = {
    "model": "sonnet",
    "effort": "high",
    "budget_usd": 2.0,
    "timeout_s": 1200,
    "read_only": False,
    "evidence_required": False,
    "description": "Code behind a check.",
    "mcp": {},
    "author": "pryce",
    "reason": "tune the budget",
}


def _stored(**over):
    base = {k: BODY[k] for k in profiles.FIELDS}
    return {
        "name": "implement",
        **base,
        "updated_by": "pryce",
        "updated_at": NOW,
        "version": 3,
    } | over


@pytest.fixture(scope="module")
def api():
    app = FastAPI()
    app.include_router(agent_profiles.router, prefix="/agents")
    return TestClient(app)


@pytest.fixture
def store(monkeypatch):
    state = {"profiles": {"implement": _stored()}, "writes": [], "removed": [], "count": 5}

    async def fetch(name):
        return state["profiles"].get(name)

    async def write(name, fields, current, author, reason):
        state["writes"].append((name, fields, current, author, reason))

    async def count():
        return state["count"]

    async def remove_row(name, version, profile, author, reason):
        state["removed"].append((name, version, profile, author, reason))

    monkeypatch.setattr(profiles, "fetch", fetch)
    monkeypatch.setattr(profiles, "write", write)
    monkeypatch.setattr(profiles, "count", count)
    monkeypatch.setattr(profiles, "remove_row", remove_row)
    return state


@pytest.mark.parametrize(
    ("path", "field", "value"),
    [
        ("implement", "effort", "turbo"),
        ("implement", "model", "gpt-5"),
        ("implement", "budget_usd", 0.01),
        ("implement", "budget_usd", 51),
        ("implement", "timeout_s", 30),
        ("implement", "timeout_s", 9000),
        ("implement", "reason", "hi"),
        ("implement", "author", "someone"),
        ("Bad_Name", "reason", "valid reason"),
        ("x", "reason", "valid reason"),
    ],
)
def test_put_rejects_invalid_input(api, store, path, field, value):
    response = api.put(f"/agents/profiles/{path}", json={**BODY, field: value})
    assert response.status_code == 422
    assert store["writes"] == []


def test_put_accepts_a_full_claude_model_id(api, store):
    body = {**BODY, "model": "claude-sonnet-4-5", "effort": "max"}
    assert api.put("/agents/profiles/implement", json=body).status_code == 200


def test_a_stale_expected_version_gives_409(api, store):
    response = api.put("/agents/profiles/implement", json={**BODY, "expected_version": 2})
    assert response.status_code == 409
    assert store["writes"] == []


def test_a_save_that_changes_nothing_gives_422(api, store):
    response = api.put("/agents/profiles/implement", json=BODY)
    assert response.status_code == 422
    assert store["writes"] == []


def test_a_save_writes_history_with_author_and_reason(api, store):
    body = {**BODY, "budget_usd": 3.0, "author": "agent:claude", "expected_version": 3}
    assert api.put("/agents/profiles/implement", json=body).status_code == 200
    [(name, fields, current, author, reason)] = store["writes"]
    assert (name, author, reason) == ("implement", "agent:claude", "tune the budget")
    assert fields["budget_usd"] == 3.0
    assert set(fields) == set(profiles.FIELDS)
    assert current["version"] == 3


def test_a_new_profile_is_written_without_a_current_row(api, store):
    body = {**BODY, "author": "dispatch:run-1"}
    api.put("/agents/profiles/scout", json=body)
    [(name, _, current, author, _)] = store["writes"]
    assert (name, current, author) == ("scout", None, "dispatch:run-1")


def test_deleting_implement_is_refused(api, store):
    response = api.delete(
        "/agents/profiles/implement", params={"author": "pryce", "reason": "clean up"}
    )
    assert response.status_code == 409
    assert store["removed"] == []


def test_deleting_the_last_profile_is_refused(api, store):
    store["profiles"]["solo"] = _stored(name="solo")
    store["count"] = 1
    response = api.delete("/agents/profiles/solo", params={"author": "pryce", "reason": "clean up"})
    assert response.status_code == 409
    assert store["removed"] == []


def test_deleting_a_profile_writes_a_removed_history_row(api, store):
    store["profiles"]["smoke"] = _stored(name="smoke")
    response = api.delete("/agents/profiles/smoke", params={"author": "pryce", "reason": "unused"})
    assert response.status_code == 200
    [(name, version, profile, author, reason)] = store["removed"]
    assert (name, version, author, reason) == ("smoke", 4, "pryce", "unused")
    assert set(profile) == set(profiles.FIELDS)


async def test_a_dispatch_plan_post_carries_project_and_board_plan(monkeypatch):
    seen = []

    async def upsert_plan(*args):
        seen.append(args)

    monkeypatch.setattr(dispatch, "upsert_plan", upsert_plan)
    body = dispatch_router.PlanIn(
        plan="ui-pass",
        repo="tharpep/sazed",
        base="main",
        host="desktop",
        tasks=[{"id": "api"}],
        project="agent-stack",
        board_plan="dispatch-profiles",
    )
    await dispatch_router.post_plan(body)
    assert seen[0][-2:] == ("agent-stack", "dispatch-profiles")


def _plan(name, project):
    return {
        "plan": name,
        "repo": "r",
        "base": "main",
        "host": "h",
        "tasks": [{"id": "t"}],
        "project": project,
        "board_plan": "dispatch-profiles",
        "updated_at": NOW,
    }


async def _no_rows(plan=None):
    return []


async def test_dispatch_plan_summary_returns_project_and_board_plan(monkeypatch):
    async def plan_rows():
        return [_plan("ui-pass", "agent-stack")]

    monkeypatch.setattr(dispatch, "plan_rows", plan_rows)
    monkeypatch.setattr(dispatch, "rows_for", _no_rows)
    [summary] = await dispatch.list_plans()
    assert (summary["project"], summary["board_plan"]) == ("agent-stack", "dispatch-profiles")


async def test_project_detail_includes_dispatch_runs(monkeypatch):
    async def get_project(slug):
        return {"slug": slug}

    async def plan_rows():
        return [_plan("a", "agent-stack"), _plan("b", "other")]

    monkeypatch.setattr(projects, "get_project", get_project)
    monkeypatch.setattr(dispatch, "plan_rows", plan_rows)
    monkeypatch.setattr(dispatch, "rows_for", _no_rows)
    result = await board.get_project("agent-stack")
    assert result["dispatch_runs"] == [
        {"plan": "a", "updated_at": NOW, "tasks_total": 1, "states": {"planned": 1}}
    ]
