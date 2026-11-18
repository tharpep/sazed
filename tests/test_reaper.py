"""Stuck-run reaper (seam 4): its own timer ends silent jobs and dispatch tasks, once each."""

import uuid

from app.board import dispatch
from app.config import Settings, settings
from app.jobs import reaper, service, store
from app.learning import service as learning
from app.learning.classes import CLASSES, classify
from app.routers import dispatch as dispatch_router

JOB_ID = uuid.UUID("44444444-4444-4444-4444-444444444444")


class _Pool:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.calls = []

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        return self.rows

    async def execute(self, sql, *args):
        self.calls.append((sql, args))


def _reaped_job():
    return {
        "id": JOB_ID,
        "status": "timed_out",
        "error": "worker stopped reporting",
        "profile": "change",
        "params": {},
        "meta": {},
    }


async def test_a_pass_finishes_each_silent_job_without_any_claim(monkeypatch):
    """S4-1: the reaper, not /worker/claim, expires the job and runs the finish hooks."""
    finished, expired = [], []

    async def expire_stale():
        expired.append(True)
        return [_reaped_job()]

    async def on_finished(job):
        finished.append(job["id"])

    async def reap_silent(grace_s):
        return []

    monkeypatch.setattr(store, "expire_stale", expire_stale)
    monkeypatch.setattr(service, "on_finished", on_finished)
    monkeypatch.setattr(dispatch, "reap_silent", reap_silent)
    await reaper.reap_once()
    assert expired == [True]
    assert finished == [JOB_ID]


async def test_a_second_pass_that_reaps_nothing_finishes_nothing(monkeypatch):
    """S4-4: on_finished runs only for rows this pass's UPDATE returned."""
    finished = []

    async def nothing(*args):
        return []

    async def on_finished(job):
        finished.append(job)

    monkeypatch.setattr(store, "expire_stale", nothing)
    monkeypatch.setattr(service, "on_finished", on_finished)
    monkeypatch.setattr(dispatch, "reap_silent", nothing)
    await reaper.reap_once()
    assert finished == []


async def test_a_failing_job_pass_still_reaps_dispatch(monkeypatch):
    reaped = []

    async def broken():
        raise RuntimeError("db down")

    async def reap_silent(grace_s):
        reaped.append(grace_s)
        return [{"plan": "p", "task": "t", "line": "no heartbeat since x"}]

    monkeypatch.setattr(store, "expire_stale", broken)
    monkeypatch.setattr(dispatch, "reap_silent", reap_silent)
    await reaper.reap_once()
    assert reaped == [settings.dispatch_heartbeat_grace_s]


async def test_expire_stale_only_takes_running_jobs_past_the_grace_setting(monkeypatch):
    """S4-4 and S4-6: the UPDATE re-checks status = 'running', so a concurrent second pass gets
    nothing back; the grace is the setting, a parameter rather than SQL text."""
    pool = _Pool()
    monkeypatch.setattr(store, "get_pool", lambda: pool)
    monkeypatch.setattr(store.settings, "job_heartbeat_grace_s", 77)
    await store.expire_stale()
    sql, args = pool.calls[0]
    assert "WHERE status = 'running'" in sql
    assert "RETURNING *" in sql
    assert args == (77,)


def test_thresholds_are_settings_whose_defaults_keep_jobs_as_before():
    """S4-6: a job still gets the 120 s it always had, and grace plus interval stays under the
    five minutes S4-1 and S4-2 allow."""
    defaults = Settings(_env_file=None)
    assert defaults.job_heartbeat_grace_s == 120
    assert defaults.job_heartbeat_grace_s + defaults.reaper_interval_s < 300
    assert defaults.dispatch_heartbeat_grace_s + defaults.reaper_interval_s < 300


async def test_dispatch_reap_fails_silent_running_tasks_once_and_says_since_when(monkeypatch):
    """S4-2 and S4-4: a running task with no row or beat inside the grace gets a failed row
    saying since when; its ts is derived from the running row, so a second reaper's insert hits
    the unique key and does nothing."""
    pool = _Pool([{"plan": "p", "task": "t", "line": "no heartbeat since 2026-11-18 10:00:00 UTC"}])
    monkeypatch.setattr(dispatch, "get_pool", lambda: pool)
    out = await dispatch.reap_silent(150)
    sql, args = pool.calls[0]
    assert args == (150,)
    assert "l.state = 'running'" in sql
    assert "GREATEST(l.received_at, h.beat_at)" in sql
    assert "ts + 0.001, 'failed'" in sql
    assert "'no heartbeat since '" in sql
    assert "'stop_reason', 'no_heartbeat'" in sql
    assert "ON CONFLICT (plan, task, ts, state) DO NOTHING" in sql
    assert out == [{"plan": "p", "task": "t", "line": "no heartbeat since 2026-11-18 10:00:00 UTC"}]


async def test_a_heartbeat_refreshes_the_tasks_beat(monkeypatch):
    """S4-3: the endpoint dispatch calls stores one row per task, stamped by Sazed's clock."""
    pool = _Pool()
    monkeypatch.setattr(dispatch, "get_pool", lambda: pool)
    body = dispatch_router.HeartbeatIn(plan="p", task="t")
    assert await dispatch_router.post_heartbeat(body) == {"ok": True}
    sql, args = pool.calls[0]
    assert "ON CONFLICT (plan, task) DO UPDATE SET beat_at = NOW()" in sql
    assert args == ("p", "t")


async def test_a_reaped_job_is_a_stop_and_never_proposes_a_rule(monkeypatch):
    """S4-5: the reaper's job lands in a stop class, so learning never opens a guard item."""
    proposed, classed = [], []

    async def set_class(job_id, cls):
        classed.append(cls)

    async def maybe_propose(cls):
        proposed.append(cls)

    monkeypatch.setattr(learning.store, "set_class", set_class)
    monkeypatch.setattr(learning, "maybe_propose", maybe_propose)
    assert classify(_reaped_job()) == "worker_lost"
    assert CLASSES["worker_lost"].family == "stop"
    assert await learning.record(_reaped_job()) == "worker_lost"
    assert classed == ["worker_lost"]
    assert proposed == []
