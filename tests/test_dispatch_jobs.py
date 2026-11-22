"""D7 stage 1: a dispatch plan submitted as one worker job that waits for approval. The plan rides in
the job's params; the worker key writes a plan's rows only while its dispatch job runs."""

import json
import uuid

from app.jobs import store

JOB_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")


async def test_create_job_inserts_the_same_row_through_the_pool(monkeypatch):
    seen = []

    class Pool:
        async def fetchrow(self, sql, *args):
            seen.append((sql, args))
            return {"id": JOB_ID, "params": args[6]}

    monkeypatch.setattr(store, "get_pool", lambda: Pool())
    job = await store.create_job(
        kind="code_task",
        agent="claude",
        model="sonnet",
        profile="change",
        title="t",
        prompt="p",
        params={"repo": "tharpep/sazed"},
        requested_by="console",
    )
    assert job == {"id": JOB_ID, "params": {"repo": "tharpep/sazed"}}
    [(sql, args)] = seen
    assert "INSERT INTO jobs" in sql
    assert args[:5] == ("code_task", "claude", "sonnet", "change", "t")
    assert args[5:8] == ("p", json.dumps({"repo": "tharpep/sazed"}), "queued")
    assert args[8:] == (None, "console", store.PRIORITY_PRYCE, None)
