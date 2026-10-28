"""Claim priority (plans/10 P1.3): jobs Pryce starts are claimed before jobs Sazed starts itself."""

import re

from app.jobs import store
from app.migrations import _MIGRATIONS_DIR
from app.policy.decide import _CAPPED_PREFIXES


def test_pryce_first_then_sazed():
    for requested_by in ("console", "mcp", "session:abc", "chat:abc"):
        assert store.claim_priority(requested_by) == store.PRIORITY_PRYCE
    for requested_by in ("automation-run:1", "think:x", "ci-fix:1", "sazed"):
        assert store.claim_priority(requested_by) == store.PRIORITY_SAZED
    assert store.PRIORITY_PRYCE < store.PRIORITY_SAZED


def test_backfill_uses_the_same_split_as_the_cap():
    sql = (_MIGRATIONS_DIR / "011_job_priority.sql").read_text(encoding="utf-8")
    prefixes = {p.rstrip("%") for p in re.findall(r"requested_by LIKE '([^']+)'", sql)}
    assert prefixes == set(_CAPPED_PREFIXES)


async def test_new_jobs_carry_their_priority(monkeypatch):
    sent = {}

    class Pool:
        async def fetchrow(self, sql, *args):
            sent["sql"], sent["args"] = sql, args
            return {"id": 1}

    monkeypatch.setattr(store, "get_pool", lambda: Pool())
    await store.create_job(kind="research", agent="agy", model="m", profile="research",
                           title="t", prompt="p", params={}, requested_by="automation-run:1")
    assert "priority" in sent["sql"] and sent["args"][-1] == store.PRIORITY_SAZED


async def test_claim_orders_by_priority_then_age_in_one_query(monkeypatch):
    sent = {}

    class Pool:
        async def fetchrow(self, sql, *args):
            sent["sql"] = sql
            return None

    monkeypatch.setattr(store, "get_pool", lambda: Pool())
    assert await store.claim_next(["agy"]) is None
    assert "ORDER BY priority, created_at" in sent["sql"]
    assert "FOR UPDATE SKIP LOCKED" in sent["sql"]
