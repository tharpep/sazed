"""Read-only registry twins under the worker key: auth, parity with the console, no secrets."""

from datetime import UTC, datetime

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.board import mcp_servers, profiles
from app.config import settings
from app.dependencies import verify_api_key
from app.routers import agent_profiles, worker

NOW = datetime.now(UTC)
PROFILE = {
    "name": "implement",
    "engine": "claude",
    "capability": "change",
    "model": "sonnet",
    "effort": "high",
    "budget_usd": 2.0,
    "timeout_s": 1200,
    "max_tool_calls": None,
    "evidence_required": False,
    "description": "Code behind a check.",
    "mcp": {"docs": ["*"]},
    "read_only": False,
    "updated_by": "pryce",
    "updated_at": NOW,
    "version": 3,
}
NO_BUDGET = {**PROFILE, "name": "sweep", "budget_usd": None}
SERVER = {
    "name": "docs",
    "transport": "http",
    "url": "https://mcp.example.com/mcp",
    "command": None,
    "args": [],
    "headers": {"Authorization": "${DOCS_TOKEN}"},
    "agent_safe": True,
    "description": "Example docs",
    "version": 1,
    "updated_by": "pryce",
    "updated_at": NOW,
}
PATHS = ("/agents/profiles", "/agents/mcp-servers")


@pytest.fixture
def rows(monkeypatch):
    state = {"profiles": [PROFILE, NO_BUDGET], "servers": [SERVER]}

    async def list_profiles():
        return state["profiles"]

    async def list_servers():
        return state["servers"]

    monkeypatch.setattr(profiles, "list_profiles", list_profiles)
    monkeypatch.setattr(mcp_servers, "list_servers", list_servers)
    monkeypatch.setattr(settings, "worker_key", "w-key")
    monkeypatch.setattr(settings, "api_key", "console-key")
    return state


@pytest.fixture(scope="module")
def api():
    app = FastAPI()
    app.include_router(
        agent_profiles.router, prefix="/agents", dependencies=[Depends(verify_api_key)]
    )
    app.include_router(worker.router, prefix="/worker")
    return TestClient(app)


@pytest.mark.parametrize("path", PATHS)
def test_the_worker_key_reads_what_the_console_reads_in_the_new_shape(api, rows, path):
    console = api.get(path, headers={"X-API-Key": "console-key", "X-Sazed-Shape": "2"})
    mine = api.get(f"/worker{path}", headers={"X-Worker-Key": "w-key"})
    assert console.status_code == mine.status_code == 200
    assert mine.json() == console.json()


def test_profiles_come_in_the_new_shape_with_every_row(api, rows):
    body = api.get("/worker/agents/profiles", headers={"X-Worker-Key": "w-key"}).json()
    assert [p["name"] for p in body] == ["implement", "sweep"]
    assert body[0]["capability"] == "change"


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize(
    "headers", [{}, {"X-API-Key": "console-key"}, {"X-Worker-Key": "console-key"}]
)
def test_the_console_key_and_no_key_are_refused(api, rows, path, headers):
    assert api.get(f"/worker{path}", headers=headers).status_code == 401


@pytest.mark.parametrize("path", PATHS)
def test_an_unset_worker_key_answers_503(api, rows, monkeypatch, path):
    monkeypatch.setattr(settings, "worker_key", "")
    assert api.get(f"/worker{path}", headers={"X-Worker-Key": ""}).status_code == 503


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("method", ["put", "post", "delete"])
def test_no_write_takes_the_worker_key(api, rows, path, method):
    response = api.request(method, f"/worker{path}/docs", headers={"X-Worker-Key": "w-key"})
    assert response.status_code in (404, 405)


def test_a_literal_header_value_never_reaches_the_worker(api, rows):
    """A raw credential in a header is dropped; ${VAR} references pass for the worker to fill."""
    leaked = {"Authorization": "Bearer sk-live-123", "X-Team": "${TEAM_ID}"}
    rows["servers"] = [{**SERVER, "headers": leaked}]
    body = api.get("/worker/agents/mcp-servers", headers={"X-Worker-Key": "w-key"}).json()
    assert body[0]["headers"] == {"X-Team": "${TEAM_ID}"}
    assert "sk-live-123" not in str(body)
