"""Voice turn log: POST validation and insert, the summary query, and the timing stamps."""

import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.agent.loop import _TurnTiming
from app.routers import voice

ROW_ID = uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")


class FakePool:
    def __init__(self, row):
        self.row, self.calls = row, []

    async def fetchrow(self, sql, *args):
        self.calls.append((sql, args))
        return self.row


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(voice.router, prefix="/voice")
    return TestClient(app)


def _use(monkeypatch, row):
    pool = FakePool(row)
    monkeypatch.setattr(voice, "get_pool", lambda: pool)
    return pool


def test_post_turn_inserts_the_row_and_returns_its_id(client, monkeypatch):
    """A valid turn is inserted with its fields in column order and answers 201 with the new id."""
    pool = _use(monkeypatch, {"id": ROW_ID})
    res = client.post(
        "/voice/turns", json={"outcome": "spoken", "kiosk": True, "stt_ms": 420, "engine": "fish"}
    )
    assert res.status_code == 201
    assert res.json() == {"id": str(ROW_ID)}
    sql, args = pool.calls[0]
    assert sql.startswith("INSERT INTO voice_turns")
    columns = sql.split("(", 1)[1].split(")", 1)[0].split(", ")
    assert dict(zip(columns, args, strict=True)) == {
        **{c: None for c in voice.MS_COLUMNS},
        "session_id": None,
        "kiosk": True,
        "outcome": "spoken",
        "tool_turn": False,
        "engine": "fish",
        "stt_ms": 420,
    }


def test_post_turn_rejects_a_negative_ms_value(client, monkeypatch):
    """A negative duration is a client bug, so it fails validation before touching the database."""
    pool = _use(monkeypatch, {"id": ROW_ID})
    res = client.post("/voice/turns", json={"outcome": "spoken", "stt_ms": -1})
    assert res.status_code == 422
    assert pool.calls == []


def test_post_turn_rejects_an_unknown_outcome(client, monkeypatch):
    """Only the five outcomes the table's CHECK allows pass validation."""
    pool = _use(monkeypatch, {"id": ROW_ID})
    res = client.post("/voice/turns", json={"outcome": "mumbled"})
    assert res.status_code == 422
    assert pool.calls == []


def _summary_row():
    row = {"turns": 3}
    for c in voice.MS_COLUMNS:
        row.update({f"{c}_n": 0, f"{c}_p50": None, f"{c}_p90": None})
    row.update(stt_ms_n=3, stt_ms_p50=400.0, stt_ms_p90=612.4)
    return row


def test_summary_without_kiosk_filters_only_by_window(client, monkeypatch):
    """Without kiosk the query binds only the day window; percentiles come back rounded."""
    pool = _use(monkeypatch, _summary_row())
    res = client.get("/voice/turns/summary?days=14")
    sql, args = pool.calls[0]
    assert args == (14,)
    assert "kiosk" not in sql
    body = res.json()
    assert body["days"] == 14
    assert body["turns"] == 3
    assert body["slices"]["stt_ms"] == {"n": 3, "p50": 400, "p90": 612}
    assert body["slices"]["setup_ms"] == {"n": 0, "p50": None, "p90": None}
    assert set(body["slices"]) == set(voice.MS_COLUMNS)


@pytest.mark.parametrize("flag", [True, False])
def test_summary_with_kiosk_adds_the_filter(client, monkeypatch, flag):
    """kiosk=true and kiosk=false each bind their value as the second query parameter."""
    pool = _use(monkeypatch, _summary_row())
    client.get(f"/voice/turns/summary?kiosk={str(flag).lower()}")
    sql, args = pool.calls[0]
    assert "kiosk = $2" in sql
    assert args == (7, flag)


def test_timing_skips_thinking_blocks_and_only_counts_the_first_call():
    """ttft ends at the first non-thinking block of the first call; later calls never move it."""
    t = _TurnTiming()
    t.llm_call_started()
    t.block_started("thinking")
    assert t.payload()["timing"]["llm_ttft_ms"] is None
    t.block_started("text")
    t.text_yielded()
    stamps = t.payload()["timing"]
    t.llm_call_started()
    t.block_started("tool_use")
    assert t.payload()["timing"] == stamps
    assert all(isinstance(v, int) for v in stamps.values())


def test_timing_without_an_llm_call_reports_nulls():
    """A turn that never reached the model reports null for every stage it skipped."""
    assert _TurnTiming().payload()["timing"] == {
        "setup_ms": None,
        "llm_ttft_ms": None,
        "first_text_ms": None,
    }
