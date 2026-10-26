"""Idle-session sweep: process each quiet chat once, stamp it, and leave failures for a retry."""

import uuid
from datetime import UTC, datetime

import pytest

from app.agent import idle_sessions


class _FakePool:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    async def fetch(self, query, *args):
        self.calls.append((query, args))
        return self.rows


@pytest.fixture
def calls(monkeypatch):
    recorded = {"processed": [], "marked": []}

    async def fake_get_session(session_id):
        return recorded.get("messages", [{"role": "user", "content": "hi"}])

    async def fake_process_session(session_id, messages, **kwargs):
        recorded["processed"].append((session_id, kwargs))
        return {"facts_extracted": 1, "procedure_proposed": None, "kb_ingested": True}

    async def fake_mark_processed(session_id):
        recorded["marked"].append(session_id)

    monkeypatch.setattr(idle_sessions, "get_session", fake_get_session)
    monkeypatch.setattr(idle_sessions, "process_session", fake_process_session)
    monkeypatch.setattr(idle_sessions, "mark_processed", fake_mark_processed)
    return recorded


def _row():
    return {
        "id": uuid.uuid4(),
        "created_at": datetime(2026, 10, 20, 12, tzinfo=UTC),
        "last_activity": datetime(2026, 10, 20, 13, tzinfo=UTC),
    }


async def test_due_sessions_query_uses_settings(monkeypatch):
    pool = _FakePool([_row()])
    monkeypatch.setattr(idle_sessions, "get_pool", lambda: pool)
    monkeypatch.setattr(idle_sessions.settings, "idle_session_hours", 3)

    rows = await idle_sessions._due_sessions()

    assert len(rows) == 1
    query, args = pool.calls[0]
    assert "processed_at IS NULL" in query
    assert "session_type = 'chat'" in query
    assert args == (3, idle_sessions.BATCH_SIZE)


async def test_process_passes_timestamps_and_stamps(calls):
    row = _row()
    await idle_sessions._process(row)

    session_id, kwargs = calls["processed"][0]
    assert session_id == str(row["id"])
    assert kwargs["session_dt"] == row["last_activity"]
    assert kwargs["session_start"] == row["created_at"]
    assert kwargs["session_type"] == "chat"
    assert calls["marked"] == [str(row["id"])]


async def test_empty_session_is_stamped_without_processing(calls):
    calls["messages"] = []
    row = _row()
    await idle_sessions._process(row)

    assert calls["processed"] == []
    assert calls["marked"] == [str(row["id"])]


async def test_failed_processing_is_not_stamped(calls, monkeypatch):
    async def boom(*args, **kwargs):
        raise RuntimeError("model down")

    monkeypatch.setattr(idle_sessions, "process_session", boom)
    with pytest.raises(RuntimeError):
        await idle_sessions._process(_row())
    # Unstamped, so the next sweep retries it.
    assert calls["marked"] == []


async def test_start_is_off_without_a_worker(monkeypatch):
    monkeypatch.setattr(idle_sessions.settings, "worker_key", "")
    await idle_sessions.start()
    assert idle_sessions._task is None
