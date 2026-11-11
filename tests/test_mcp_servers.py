"""MCP server registry and per-profile grants: validation, versioned saves, guarded deletes."""

from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.board import mcp_servers, profiles
from app.routers import agent_profiles

NOW = datetime.now(UTC)
SERVER = {
    "transport": "http",
    "url": "https://mcp.example.com/mcp",
    "command": None,
    "args": [],
    "headers": {"Authorization": "${EXAMPLE_TOKEN}"},
    "agent_safe": True,
    "description": "Example docs",
}
BODY = {**SERVER, "author": "pryce", "reason": "add example"}
PROFILE = {
    "model": "sonnet",
    "effort": "high",
    "budget_usd": 2.0,
    "timeout_s": 1200,
    "read_only": False,
    "evidence_required": False,
    "description": "Code behind a check.",
    "mcp": {},
    "author": "pryce",
    "reason": "grant docs",
}


def _server(**over):
    stored = {"name": "example", **SERVER, "version": 2, "updated_by": "pryce"}
    return stored | {"updated_at": NOW} | over


def _profile(**over):
    base = {k: PROFILE[k] for k in profiles.FIELDS}
    stored = {"name": "implement", **base, "updated_by": "pryce", "updated_at": NOW}
    return stored | {"version": 3} | over


@pytest.fixture(scope="module")
def api():
    app = FastAPI()
    app.include_router(agent_profiles.router, prefix="/agents")
    return TestClient(app)


@pytest.fixture
def store(monkeypatch):
    state = {
        "servers": {
            "example": _server(),
            "unsafe": _server(name="unsafe", agent_safe=False),
        },
        "profiles": {"implement": _profile()},
        "granting": [],
        "server_writes": [],
        "server_removed": [],
        "profile_writes": [],
    }

    async def server_fetch(name):
        return state["servers"].get(name)

    async def server_write(name, fields, current, author, reason):
        state["server_writes"].append((name, fields, current, author, reason))
        state["servers"][name] = _server(name=name, **fields, updated_by=author)

    async def granted_by(name):
        return state["granting"]

    async def server_remove(name, version, server, author, reason):
        state["server_removed"].append((name, version, server, author, reason))

    async def profile_fetch(name):
        return state["profiles"].get(name)

    async def profile_write(name, fields, current, author, reason):
        state["profile_writes"].append((name, fields, current, author, reason))

    monkeypatch.setattr(mcp_servers, "fetch", server_fetch)
    monkeypatch.setattr(mcp_servers, "write", server_write)
    monkeypatch.setattr(mcp_servers, "granted_by", granted_by)
    monkeypatch.setattr(mcp_servers, "remove_row", server_remove)
    monkeypatch.setattr(profiles, "fetch", profile_fetch)
    monkeypatch.setattr(profiles, "write", profile_write)
    return state


@pytest.mark.parametrize(
    ("path", "changes"),
    [
        ("Bad_Name", {}),
        ("x", {}),
        ("fresh", {"url": None}),
        ("fresh", {"url": "ftp://mcp.example.com"}),
        ("fresh", {"transport": "stdio", "command": None}),
        ("fresh", {"transport": "websocket"}),
        ("fresh", {"headers": {"Authorization": "Bearer abc123"}}),
        ("fresh", {"headers": {"Authorization": "Bearer ${EXAMPLE_TOKEN}"}}),
        ("fresh", {"headers": {"Authorization": "${lower}"}}),
        ("fresh", {"author": "someone"}),
        ("fresh", {"reason": "hi"}),
    ],
)
def test_server_put_rejects_invalid_input(api, store, path, changes):
    response = api.put(f"/agents/mcp-servers/{path}", json={**BODY, **changes})
    assert response.status_code == 422
    assert store["server_writes"] == []


def test_a_stdio_server_saves_with_command_and_args(api, store):
    body = {**BODY, "transport": "stdio", "command": "npx", "args": ["-y", "pkg"], "url": None}
    assert api.put("/agents/mcp-servers/local", json=body).status_code == 200
    [(_, fields, current, _, _)] = store["server_writes"]
    assert (fields["command"], fields["args"], current) == ("npx", ["-y", "pkg"], None)


def test_a_stale_server_version_gives_409(api, store):
    response = api.put("/agents/mcp-servers/example", json={**BODY, "expected_version": 1})
    assert response.status_code == 409
    assert store["server_writes"] == []


def test_a_server_save_that_changes_nothing_gives_422(api, store):
    response = api.put("/agents/mcp-servers/example", json=BODY)
    assert response.status_code == 422
    assert store["server_writes"] == []


def test_a_server_save_writes_history_with_author_and_reason(api, store):
    body = {**BODY, "description": "Changed", "author": "agent:claude", "expected_version": 2}
    assert api.put("/agents/mcp-servers/example", json=body).status_code == 200
    [(name, fields, current, author, reason)] = store["server_writes"]
    assert (name, author, reason) == ("example", "agent:claude", "add example")
    assert set(fields) == set(mcp_servers.FIELDS)
    assert current["version"] == 2


def test_deleting_a_server_a_profile_grants_is_refused(api, store):
    store["granting"] = ["implement"]
    response = api.delete(
        "/agents/mcp-servers/example", params={"author": "pryce", "reason": "clean up"}
    )
    assert response.status_code == 409
    assert store["server_removed"] == []


def test_deleting_an_ungranted_server_writes_a_removed_history_row(api, store):
    response = api.delete(
        "/agents/mcp-servers/example", params={"author": "pryce", "reason": "clean up"}
    )
    assert response.status_code == 200
    [(name, version, server, _, _)] = store["server_removed"]
    assert (name, version, set(server)) == ("example", 3, set(mcp_servers.FIELDS))


def test_a_grant_of_an_unknown_server_is_refused(api, store):
    body = {**PROFILE, "mcp": {"ghost": ["*"]}}
    assert api.put("/agents/profiles/implement", json=body).status_code == 422
    assert store["profile_writes"] == []


def test_a_grant_of_a_non_agent_safe_server_is_refused(api, store):
    body = {**PROFILE, "mcp": {"unsafe": ["*"]}}
    assert api.put("/agents/profiles/implement", json=body).status_code == 422
    assert store["profile_writes"] == []


@pytest.mark.parametrize(
    "mcp",
    [
        {"example": ["bad tool"]},
        {"example": ["x" * 65]},
        {"example": []},
        {"example": ["*", "query"]},
        {"Bad": ["query"]},
    ],
)
def test_bad_tool_grants_are_refused(api, store, mcp):
    assert api.put("/agents/profiles/implement", json={**PROFILE, "mcp": mcp}).status_code == 422
    assert store["profile_writes"] == []


def test_mcp_counts_as_a_change_and_lands_in_the_profile_history(api, store):
    body = {**PROFILE, "mcp": {"example": ["query-docs"]}, "expected_version": 3}
    assert api.put("/agents/profiles/implement", json=body).status_code == 200
    [(_, fields, current, _, _)] = store["profile_writes"]
    assert fields["mcp"] == {"example": ["query-docs"]}
    assert current["mcp"] == {}


def test_a_profile_save_without_mcp_defaults_to_none_granted(api, store):
    body = {k: v for k, v in PROFILE.items() if k != "mcp"} | {"budget_usd": 3.0}
    assert api.put("/agents/profiles/implement", json=body).status_code == 200
    assert store["profile_writes"][0][1]["mcp"] == {}


def test_an_unchanged_mcp_grant_is_a_no_op(api, store):
    store["profiles"]["implement"] = _profile(mcp={"example": ["*"]})
    body = {**PROFILE, "mcp": {"example": ["*"]}}
    assert api.put("/agents/profiles/implement", json=body).status_code == 422
    assert store["profile_writes"] == []


AGENT = {"author": "agent:claude", "reason": "propose server"}
STDIO = {"transport": "stdio", "command": "npx", "args": ["-y", "pkg"], "url": None}


def test_an_agent_added_command_is_saved_but_not_agent_safe(api, store):
    body = {**BODY, **STDIO, **AGENT, "agent_safe": True}
    response = api.put("/agents/mcp-servers/local", json=body)
    assert response.status_code == 200
    assert "approve it in the Sazed console" in response.json()["held"]
    [(_, fields, _, author, reason)] = store["server_writes"]
    assert (fields["command"], fields["agent_safe"], author) == ("npx", False, "agent:claude")
    assert "held for owner approval" in reason


def test_an_agent_cannot_mark_an_entry_agent_safe(api, store):
    body = {**BODY, **AGENT, "agent_safe": True, "description": "Example docs"}
    response = api.put("/agents/mcp-servers/unsafe", json=body)
    assert response.status_code == 403
    assert "approve it in the Sazed console" in response.json()["detail"]
    assert store["server_writes"] == []


def test_an_agent_held_entry_cannot_be_granted(api, store):
    store["servers"]["local"] = _server(name="local", **STDIO, agent_safe=False)
    body = {**PROFILE, "author": "agent:claude", "mcp": {"local": ["*"]}}
    response = api.put("/agents/profiles/implement", json=body)
    assert response.status_code == 422
    assert "approve it in the Sazed console" in response.json()["detail"]
    assert store["profile_writes"] == []


def test_the_owner_can_add_a_command_and_mark_it_agent_safe(api, store):
    body = {**BODY, **STDIO, "agent_safe": True}
    response = api.put("/agents/mcp-servers/local", json=body)
    assert response.status_code == 200
    assert response.json()["agent_safe"] is True
    assert "held" not in response.json()
    [(_, fields, _, author, _)] = store["server_writes"]
    assert (fields["agent_safe"], author) == (True, "pryce")


def test_the_owner_approves_an_agent_proposed_entry(api, store):
    store["servers"]["local"] = _server(name="local", **STDIO, agent_safe=False)
    body = {**BODY, **STDIO, "agent_safe": True}
    assert api.put("/agents/mcp-servers/local", json=body).status_code == 200
    assert store["server_writes"][0][1]["agent_safe"] is True


@pytest.mark.parametrize(
    "changes",
    [
        STDIO,
        {"url": "https://evil.example.com/mcp"},
        {"headers": {"Authorization": "${GITHUB_TOKEN}"}},
    ],
)
def test_an_agent_cannot_change_the_connection_of_an_approved_entry(api, store, changes):
    response = api.put("/agents/mcp-servers/example", json={**BODY, **AGENT, **changes})
    assert response.status_code == 403
    assert "approve it in the Sazed console" in response.json()["detail"]
    assert store["server_writes"] == []


def test_an_agent_cannot_change_the_command_of_a_granted_unsafe_entry(api, store):
    store["granting"] = ["implement"]
    body = {**BODY, **STDIO, **AGENT, "agent_safe": False}
    assert api.put("/agents/mcp-servers/unsafe", json=body).status_code == 403
    assert store["server_writes"] == []


def test_an_agent_can_revise_its_own_unapproved_proposal(api, store):
    store["servers"]["local"] = _server(name="local", **STDIO, agent_safe=False)
    body = {**BODY, **STDIO, **AGENT, "args": ["-y", "other"], "agent_safe": False}
    response = api.put("/agents/mcp-servers/local", json=body)
    assert response.status_code == 200
    assert "held" not in response.json()
    assert store["server_writes"][0][1]["args"] == ["-y", "other"]


def test_an_agent_description_edit_keeps_an_approved_entry_agent_safe(api, store):
    body = {**BODY, **AGENT, "description": "Docs, reworded"}
    response = api.put("/agents/mcp-servers/example", json=body)
    assert response.status_code == 200
    [(_, fields, _, author, reason)] = store["server_writes"]
    assert (fields["agent_safe"], fields["url"], author) == (True, SERVER["url"], "agent:claude")
    assert reason == "propose server"


def test_an_approved_entry_stays_grantable_by_an_agent(api, store):
    body = {**PROFILE, "author": "agent:claude", "mcp": {"example": ["query-docs"]}}
    assert api.put("/agents/profiles/implement", json=body).status_code == 200
    assert store["profile_writes"][0][1]["mcp"] == {"example": ["query-docs"]}
