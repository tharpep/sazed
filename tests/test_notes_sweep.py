"""The background notes sweep: when it starts, and that one turn survives a failed sweep."""

import asyncio

import pytest

from app.notes import sweep


@pytest.fixture(autouse=True)
def _reset_task(monkeypatch):
    monkeypatch.setattr(sweep, "_task", None)


@pytest.fixture
def created(monkeypatch):
    tasks = []

    def fake_create_task(coro):
        coro.close()
        tasks.append(coro)
        return object()

    monkeypatch.setattr(sweep.asyncio, "create_task", fake_create_task)
    return tasks


@pytest.mark.parametrize(
    ("seconds", "gateway"),
    [(0, "http://gw"), (-5, "http://gw"), (300, "")],
)
async def test_start_creates_no_task_when_off(monkeypatch, created, seconds, gateway):
    monkeypatch.setattr(sweep.settings, "notes_sweep_seconds", seconds)
    monkeypatch.setattr(sweep.settings, "gateway_url", gateway)
    await sweep.start()
    assert created == []
    assert sweep._task is None


async def test_start_creates_a_task_when_set(monkeypatch, created):
    monkeypatch.setattr(sweep.settings, "notes_sweep_seconds", 300)
    monkeypatch.setattr(sweep.settings, "gateway_url", "http://gw")
    await sweep.start()
    assert len(created) == 1
    assert sweep._task is not None


def _one_turn(monkeypatch):
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(sweep.asyncio, "sleep", fake_sleep)
    return sleeps


async def test_a_turn_calls_the_sweep(monkeypatch):
    monkeypatch.setattr(sweep.settings, "notes_sweep_seconds", 300)
    calls = []

    async def fake_sweep_once():
        calls.append(1)
        return {"seen": 1, "filed": [{"path": "a"}], "waiting": 0, "failed": 0}

    monkeypatch.setattr(sweep, "sweep_once", fake_sweep_once)
    sleeps = _one_turn(monkeypatch)
    with pytest.raises(asyncio.CancelledError):
        await sweep._loop()
    assert calls == [1]
    assert sleeps == [300, 300]


async def test_the_sleep_is_at_least_a_minute(monkeypatch):
    monkeypatch.setattr(sweep.settings, "notes_sweep_seconds", 5)

    async def fake_sweep_once():
        return {"seen": 0, "filed": [], "waiting": 0, "failed": 0}

    monkeypatch.setattr(sweep, "sweep_once", fake_sweep_once)
    sleeps = _one_turn(monkeypatch)
    with pytest.raises(asyncio.CancelledError):
        await sweep._loop()
    assert sleeps == [60, 60]


async def test_a_failed_sweep_does_not_end_the_loop(monkeypatch):
    monkeypatch.setattr(sweep.settings, "notes_sweep_seconds", 300)
    calls = []

    async def fake_sweep_once():
        calls.append(1)
        raise RuntimeError("gateway down")

    monkeypatch.setattr(sweep, "sweep_once", fake_sweep_once)
    sleeps = _one_turn(monkeypatch)
    with pytest.raises(asyncio.CancelledError):
        await sweep._loop()
    assert calls == [1]
    assert len(sleeps) == 2
