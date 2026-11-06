"""Claude usage routes: key auth, batch limits and idempotence, and the read response shapes."""

from contextlib import asynccontextmanager
from datetime import UTC, date, datetime

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.claude_usage import store
from app.config import settings
from app.dependencies import verify_api_key
from app.routers import claude_usage

KEY = "usage-key"
API_KEY = "console-key"
SESSION = {
    "session_id": "s",
    "project": None,
    "title": None,
    "agent_name": None,
    "version": None,
    "first_ts": "2026-11-03T01:00:00Z",
    "last_ts": "2026-11-03T09:00:00Z",
}


def _t(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, 3, hour, minute, tzinfo=UTC)


class FakeConn:
    def __init__(self, new_rows):
        self.new_rows = new_rows
        self.calls = []

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        if "claude_usage_sessions" in sql:
            return [{"inserted": True}] * self.new_rows
        return [{"?column?": 1}] * self.new_rows

    @asynccontextmanager
    async def transaction(self):
        yield


class FakePool:
    def __init__(self, new_rows=1):
        self.conn = FakeConn(new_rows)

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "claude_usage_key", KEY)
    monkeypatch.setattr(settings, "api_key", API_KEY)
    app = FastAPI()
    app.include_router(claude_usage.ingest_router, prefix="/claude-usage")
    app.include_router(
        claude_usage.router, prefix="/claude-usage", dependencies=[Depends(verify_api_key)]
    )
    return TestClient(app)


@pytest.fixture
def pool(monkeypatch):
    fake = FakePool()
    monkeypatch.setattr(store, "get_pool", lambda: fake)
    return fake


def _post(client, body=None, headers=None):
    return client.post("/claude-usage/batch", json=body or {"machine": "m"}, headers=headers or {})


def test_unset_key_is_503_even_with_a_header(client, monkeypatch):
    monkeypatch.setattr(settings, "claude_usage_key", "")
    assert _post(client, headers={"X-Claude-Usage-Key": ""}).status_code == 503
    assert _post(client, headers={"X-Claude-Usage-Key": KEY}).status_code == 503


def test_wrong_or_missing_key_is_401(client):
    assert _post(client, headers={"X-Claude-Usage-Key": "nope"}).status_code == 401
    assert _post(client).status_code == 401


def test_api_key_alone_does_not_pass_ingest(client):
    assert _post(client, headers={"X-API-Key": API_KEY}).status_code == 401
    assert _post(client, headers={"Authorization": f"Bearer {API_KEY}"}).status_code == 401


def test_usage_key_does_not_pass_reads(client):
    for path in ("/claude-usage/overview", "/claude-usage/sessions"):
        assert client.get(path, headers={"X-Claude-Usage-Key": KEY}).status_code == 401


def test_auth_is_checked_before_the_body(client):
    bad = {"machine": "bad machine!"}
    assert _post(client, bad, {"X-Claude-Usage-Key": "nope"}).status_code == 401


@pytest.mark.parametrize("machine", ["", "a b", "a/b", "x" * 65, "ü"])
def test_invalid_machine_is_422(client, pool, machine):
    resp = _post(client, {"machine": machine}, {"X-Claude-Usage-Key": KEY})
    assert resp.status_code == 422


def test_machine_with_allowed_punctuation_passes(client, pool):
    resp = _post(client, {"machine": "desktop-wsl_1.local"}, {"X-Claude-Usage-Key": KEY})
    assert resp.status_code == 200
    resp = _post(client, {"machine": "x" * 64}, {"X-Claude-Usage-Key": KEY})
    assert resp.status_code == 200


def _event(i):
    return {"uuid": f"e{i}", "session_id": "s", "ts": "2026-11-03T01:00:00Z", "kind": "compact"}


def test_over_5000_rows_in_total_is_413(client, pool):
    body = {
        "machine": "m",
        "events": [_event(i) for i in range(3000)],
        "prompts": [
            {
                "uuid": f"p{i}",
                "session_id": "s",
                "ts": "2026-11-03T01:00:00Z",
                "chars": 1,
                "confused": False,
            }
            for i in range(2001)
        ],
    }
    resp = _post(client, body, {"X-Claude-Usage-Key": KEY})
    assert resp.status_code == 413
    assert pool.conn.calls == []


def test_exactly_5000_rows_is_accepted(client, pool):
    body = {"machine": "m", "events": [_event(i) for i in range(5000)]}
    assert _post(client, body, {"X-Claude-Usage-Key": KEY}).status_code == 200


def test_unknown_event_kind_is_422(client, pool):
    body = {"machine": "m", "events": [{**_event(1), "kind": "other"}]}
    assert _post(client, body, {"X-Claude-Usage-Key": KEY}).status_code == 422


def test_empty_batch_reports_zero_accepted_everywhere(client, pool):
    resp = _post(client, headers={"X-Claude-Usage-Key": KEY})
    assert resp.status_code == 200
    assert resp.json() == {
        "accepted": dict.fromkeys(claude_usage.LISTS, 0),
    }
    assert pool.conn.calls == []


def test_batch_inserts_each_list_once_and_counts_new_rows(client, pool):
    body = {
        "machine": "m",
        "sessions": [SESSION],
        "events": [_event(1)],
        "slash_commands": [{"ts": "1793929483029", "project": "/p", "command": "/clear"}],
    }
    resp = _post(client, body, {"X-Claude-Usage-Key": KEY})
    assert resp.json()["accepted"] == {
        "sessions": 1,
        "prompts": 0,
        "responses": 0,
        "tool_calls": 0,
        "events": 1,
        "slash_commands": 1,
    }
    sql_by_table = {sql.split()[2]: (sql, args) for sql, args in pool.conn.calls}
    assert set(sql_by_table) == {
        "claude_usage_sessions",
        "claude_usage_events",
        "claude_usage_slash_commands",
    }
    sql, args = sql_by_table["claude_usage_slash_commands"]
    assert args[0] == "m"
    assert args[1] == [datetime.fromtimestamp(1793929483.029, UTC)]


def test_resend_inserts_nothing_new(client, monkeypatch):
    fake = FakePool(new_rows=0)
    monkeypatch.setattr(store, "get_pool", lambda: fake)
    body = {"machine": "m", "sessions": [SESSION], "events": [_event(1)]}
    resp = _post(client, body, {"X-Claude-Usage-Key": KEY})
    assert resp.json()["accepted"]["sessions"] == 0
    assert resp.json()["accepted"]["events"] == 0
    for sql, _ in fake.conn.calls:
        assert "ON CONFLICT" in sql


def test_row_inserts_ignore_conflicts_and_session_upsert_merges():
    for table, columns in store._TABLES.values():
        sql = store._insert_sql(table, columns)
        assert sql.endswith("ON CONFLICT DO NOTHING RETURNING 1")
    sql = store._UPSERT_SESSIONS
    assert "LEAST(claude_usage_sessions.first_ts" in sql
    assert "GREATEST(claude_usage_sessions.last_ts" in sql
    for field in ("project", "title", "agent_name", "version"):
        assert f"{field} = COALESCE(claude_usage_sessions.{field}, EXCLUDED.{field})" in sql


def test_merge_sessions_fills_nulls_and_widens_the_span():
    first = {**SESSION, "title": "kept", "version": None, "first_ts": _t(3), "last_ts": _t(4)}
    second = {
        **SESSION,
        "title": "ignored",
        "version": "2.1",
        "project": "/p",
        "first_ts": _t(1),
        "last_ts": _t(2),
    }
    other = {**SESSION, "session_id": "t", "first_ts": _t(1), "last_ts": _t(1)}
    merged = store.merge_sessions([first, second, other])
    assert [m["session_id"] for m in merged] == ["s", "t"]
    assert merged[0]["title"] == "kept"
    assert merged[0]["version"] == "2.1"
    assert merged[0]["project"] == "/p"
    assert merged[0]["first_ts"] == _t(1)
    assert merged[0]["last_ts"] == _t(4)


def test_duplicate_session_ids_in_one_batch_are_merged_before_the_upsert(client, pool):
    later = {**SESSION, "title": "t", "last_ts": "2026-11-03T12:00:00Z"}
    body = {"machine": "m", "sessions": [SESSION, later]}
    _post(client, body, {"X-Claude-Usage-Key": KEY})
    _, args = pool.conn.calls[0]
    assert args[1] == ["s"]
    assert args[3] == ["t"]
    assert args[7] == [datetime(2026, 11, 3, 12, tzinfo=UTC)]


def _overview_rows():
    return {
        "machines": ["desktop-wsl"],
        "prompt_days": [
            {
                "machine": "m",
                "session_id": "a",
                "day": date(2026, 10, 26),
                "prompts": 6,
                "confused": 1,
            },
            {
                "machine": "m",
                "session_id": "a",
                "day": date(2026, 11, 3),
                "prompts": 2,
                "confused": 0,
            },
            {
                "machine": "m",
                "session_id": "b",
                "day": date(2026, 11, 3),
                "prompts": 4,
                "confused": 1,
            },
        ],
        "peaks": [(100_000, 2), (250_000, 6), (500_000, 9)],
        "tool_days": [
            {"day": date(2026, 10, 26), "calls": 60, "bash_writes": 9, "edit_writes": 1},
            {"day": date(2026, 11, 3), "calls": 60, "bash_writes": 0, "edit_writes": 2},
        ],
        "response_days": [
            {"day": date(2026, 10, 27), "output_tokens": 70, "cache_read_tokens": 700},
            {"day": date(2026, 11, 2), "output_tokens": 5, "cache_read_tokens": 50},
        ],
        "models": [{"model": "opus", "n": 3}, {"model": "haiku", "n": 1}],
        "skills": [{"name": "pryce:log", "n": 4}],
        "subagents": [{"name": "general-purpose", "n": 29}],
        "slash_commands": [{"name": "/model", "n": 12}],
        "styles": [{"style": "Concise", "prompts": 4, "confused": 1}],
    }


def test_overview_shape_and_values(client, monkeypatch):
    async def fake_rows(frm, to, machine):
        fake_rows.args = (frm, to, machine)
        return _overview_rows()

    monkeypatch.setattr(store, "overview_rows", fake_rows)
    resp = client.get("/claude-usage/overview?days=7&machine=m", headers={"X-API-Key": API_KEY})
    assert resp.status_code == 200
    data = resp.json()
    assert fake_rows.args[2] == "m"
    assert (fake_rows.args[1] - fake_rows.args[0]).days == 7
    assert set(data) == {
        "range",
        "limits",
        "machines",
        "tiles",
        "weekly",
        "models",
        "skills",
        "subagents",
        "slash_commands",
        "styles",
    }
    assert set(data["range"]) == {"from", "to"}
    assert data["range"]["to"].endswith("Z")
    assert data["limits"] == {"context_soft": 200000, "context_hard": 400000}
    assert data["tiles"] == {
        "sessions": 2,
        "prompts": 12,
        "peak_context_max": 500000,
        "peak_context_median": 375000,
        "sessions_over_soft": 2,
        "sessions_over_hard": 1,
        "confusion_rate": 0.167,
        "bash_writes": 9,
        "edit_writes": 3,
        "bash_write_share": 0.75,
        "tool_calls_per_prompt": 10.0,
        "skills_used": 1,
    }
    assert data["weekly"] == [
        {
            "week": "2026-W44",
            "prompts": 6,
            "sessions": 1,
            "confusion_rate": 0.167,
            "bash_writes": 9,
            "edit_writes": 1,
            "output_tokens": 70,
            "cache_read_tokens": 700,
        },
        {
            "week": "2026-W45",
            "prompts": 6,
            "sessions": 2,
            "confusion_rate": 0.167,
            "bash_writes": 0,
            "edit_writes": 2,
            "output_tokens": 5,
            "cache_read_tokens": 50,
        },
    ]
    assert data["models"] == [
        {"model": "opus", "responses": 3, "share": 0.75},
        {"model": "haiku", "responses": 1, "share": 0.25},
    ]
    assert data["skills"] == [{"name": "pryce:log", "count": 4}]
    assert data["subagents"] == [{"type": "general-purpose", "count": 29}]
    assert data["slash_commands"] == [{"command": "/model", "count": 12}]
    assert data["styles"] == [{"style": "Concise", "prompts": 4, "confusion_rate": 0.25}]


def test_overview_with_no_data_has_null_rates_and_empty_lists(client, monkeypatch):
    async def fake_rows(frm, to, machine):
        return {
            "machines": [],
            "prompt_days": [],
            "peaks": [],
            "tool_days": [],
            "response_days": [],
            "models": [],
            "skills": [],
            "subagents": [],
            "slash_commands": [],
            "styles": [],
        }

    monkeypatch.setattr(store, "overview_rows", fake_rows)
    data = client.get("/claude-usage/overview", headers={"X-API-Key": API_KEY}).json()
    tiles = data["tiles"]
    assert tiles["sessions"] == 0
    assert tiles["peak_context_max"] is None
    assert tiles["peak_context_median"] is None
    assert tiles["confusion_rate"] is None
    assert tiles["bash_write_share"] is None
    assert tiles["tool_calls_per_prompt"] is None
    assert data["weekly"] == []
    assert data["models"] == []


@pytest.mark.parametrize("query", ["days=0", "days=366", "machine=a%20b"])
def test_read_params_are_validated(client, query):
    for path in ("overview", "sessions"):
        resp = client.get(f"/claude-usage/{path}?{query}", headers={"X-API-Key": API_KEY})
        assert resp.status_code == 422


def _session_rows():
    responses = [
        {
            "machine": "m",
            "session_id": "s",
            "ts": _t(1, i),
            "context": 1000 * (i + 1),
            "model": "opus" if i else "haiku",
        }
        for i in range(5)
    ]
    return {
        "sessions": [
            {
                "machine": "m",
                "session_id": "s",
                "project": "/p",
                "title": "T",
                "first_ts": _t(1),
                "last_ts": _t(9),
                "prompts": 54,
            },
            {
                "machine": "m",
                "session_id": "bare",
                "project": None,
                "title": None,
                "first_ts": _t(0),
                "last_ts": _t(0),
                "prompts": 1,
            },
        ],
        "responses": responses,
        "counts": [
            {"machine": "m", "session_id": "s", "kind": "subagents", "n": 3},
            {"machine": "m", "session_id": "s", "kind": "compact", "n": 1},
            {"machine": "m", "session_id": "s", "kind": "interrupt", "n": 2},
            {"machine": "m", "session_id": "s", "kind": "rejection", "n": 9},
        ],
        "confused": [
            {"machine": "m", "session_id": "s", "ts": _t(1, 3)},
            {"machine": "m", "session_id": "s", "ts": _t(0, 30)},
        ],
    }


def test_sessions_shape_and_values(client, monkeypatch):
    async def fake_rows(frm, to, machine, min_prompts):
        fake_rows.args = (machine, min_prompts)
        return _session_rows()

    monkeypatch.setattr(store, "session_rows", fake_rows)
    resp = client.get("/claude-usage/sessions?min_prompts=2", headers={"X-API-Key": API_KEY})
    assert resp.status_code == 200
    assert fake_rows.args == (None, 2)
    data = resp.json()
    assert data["limits"] == {"context_soft": 200000, "context_hard": 400000}
    first, bare = data["sessions"]
    assert set(first) == {
        "session_id",
        "machine",
        "project",
        "title",
        "first_ts",
        "last_ts",
        "prompts",
        "peak_context",
        "main_model",
        "subagents",
        "compactions",
        "interrupts",
        "confused_prompts",
        "context_series",
    }
    assert first["prompts"] == 54
    assert first["peak_context"] == 5000
    assert first["main_model"] == "opus"
    assert (first["subagents"], first["compactions"], first["interrupts"]) == (3, 1, 2)
    assert first["first_ts"] == "2026-11-03T01:00:00Z"
    assert first["confused_prompts"] == [
        {"ts": "2026-11-03T00:30:00Z", "context": 0},
        {"ts": "2026-11-03T01:03:00Z", "context": 3000},
    ]
    assert first["context_series"][0] == {"ts": "2026-11-03T01:00:00Z", "context": 1000}
    assert len(first["context_series"]) == 5
    assert bare["peak_context"] is None
    assert bare["main_model"] is None
    assert bare["context_series"] == []
    assert (bare["subagents"], bare["compactions"], bare["interrupts"]) == (0, 0, 0)


def test_sessions_series_is_capped_and_keeps_the_peak(client, monkeypatch):
    async def fake_rows(frm, to, machine, min_prompts):
        rows = _session_rows()
        rows["responses"] = [
            {
                "machine": "m",
                "session_id": "s",
                "ts": datetime.fromtimestamp(1.8e9 + i, UTC),
                "context": 999_999 if i == 123 else 5,
                "model": "opus",
            }
            for i in range(300)
        ]
        return rows

    monkeypatch.setattr(store, "session_rows", fake_rows)
    data = client.get("/claude-usage/sessions", headers={"X-API-Key": API_KEY}).json()
    series = data["sessions"][0]["context_series"]
    assert len(series) == 60
    assert 999_999 in [p["context"] for p in series]


async def test_sessions_with_no_matches_skip_the_dependent_queries(monkeypatch):
    class Pool:
        calls = 0

        async def fetch(self, sql, *args):
            Pool.calls += 1
            return []

    monkeypatch.setattr(store, "get_pool", lambda: Pool())
    out = await store.session_rows(_t(0), _t(1), None, 1)
    assert out["sessions"] == []
    assert Pool.calls == 1
