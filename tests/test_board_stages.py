"""Board stages (plans/13): a card's stage comes from its item and the last job in its chain.
The cases are from the live data on 2026-10-30, including a chain whose first job still said
"fixing" after both fix-ups had finished."""

from datetime import UTC, datetime, timedelta

import pytest

from app.board import service, stages

NOW = datetime(2026, 10, 30, 19, 0, tzinfo=UTC)


def _job(status="succeeded", finished=NOW, **meta):
    return {"status": status, "meta": meta, "finished_at": finished}


def _pr(**meta):
    return _job(pr_number=7, **meta)


def _item(status="approved", rank=None, workstream_id=None):
    return {"status": status, "rank": rank, "workstream_id": workstream_id}


@pytest.mark.parametrize(
    "job, stage",
    [
        (_job("queued"), "doing"),
        (_job("running"), "doing"),
        (_job("needs_approval"), "needs_me"),
        (_job("needs_input"), "needs_me"),
        (_job("paused", pause_reason="rate_limit"), "doing"),
        (_job("paused", pause_reason="timed_out"), "needs_me"),
        (_job("failed"), "needs_me"),
        (_job("timed_out"), "needs_me"),
        (_job("cancelled"), "needs_me"),
        (_job(), "needs_me"),
        (_pr(), "doing"),
        (_pr(ci_state="fixing"), "doing"),
        (_pr(ci_state="success"), "needs_me"),
        (_pr(ci_state="no_ci"), "needs_me"),
        (_pr(ci_state="failure"), "needs_me"),
        (_pr(ci_state="closed"), "needs_me"),
        (_pr(ci_state="merged"), "done"),
        (_pr(pr_draft=True), "needs_me"),
    ],
)
def test_job_stage(job, stage):
    assert stages.job_stage(job, NOW)[0] == stage


def test_a_pr_the_ci_watcher_gave_up_on_needs_pryce():
    job = _pr()
    job["finished_at"] = NOW - timedelta(hours=7)
    assert stages.job_stage(job, NOW) == ("needs_me", "No CI result; check the PR")


@pytest.mark.parametrize(
    "item, stage",
    [
        (_item("new"), "inbox"),
        (_item("new", rank=3), "next"),
        (_item("shortlisted"), "next"),
        (_item("new", workstream_id="w1"), "next"),
        (_item("done"), "done"),
    ],
)
def test_item_stage_without_a_job(item, stage):
    assert stages.item_stage(item, None, NOW)[0] == stage


def test_an_open_item_ignores_the_job_from_an_earlier_try():
    assert stages.item_stage(_item("new"), _job("cancelled"), NOW)[0] == "inbox"


def _chain_job(number=None, ci=None, requested_by="console"):
    meta = {"ci_state": ci} | ({"pr_number": number, "pr_url": "u"} if number else {})
    return {
        "id": number,
        "status": "succeeded",
        "requested_by": requested_by,
        "meta": meta,
        "finished_at": NOW,
    }


def test_a_card_reads_the_last_job_in_its_chain():
    chain = [
        _chain_job(17, "fixing"),
        _chain_job(None, "success", requested_by="ci-fix:1"),
    ]
    fields = service._job_fields(chain)
    assert fields["ci_state"] == "success"
    assert fields["fix_count"] == 1
    assert fields["pr_number"] == 17


def test_automation_jobs_show_only_when_they_need_pryce():
    running = {"id": 1, "status": "running", "requested_by": "automation-run:x", "meta": {}}
    waiting = running | {"id": 2, "status": "needs_approval"}
    assert not service._shows_alone(running, set())
    assert service._shows_alone(waiting, set())
    assert not service._shows_alone({**running, "requested_by": "console"}, {1})


def _done_item(title, finished):
    return {
        "id": title,
        "type": "bug",
        "status": "done",
        "title": title,
        "repos": [],
        "effort": None,
        "impact": None,
        "rank": None,
        "job_id": None,
        "workstream_id": None,
        "plan_id": None,
        "plan_section": None,
        "plan_version": None,
        "session_label": None,
        "session_branch": None,
        "pr_repo": None,
        "pr_number": None,
        "created_at": finished,
        "updated_at": datetime.now(UTC),
        "decided_at": finished,
    }


def _board(monkeypatch, items, jobs=()):
    async def all_items():
        return list(items)

    async def no_chains():
        return {}

    async def active():
        return list(jobs)

    async def no_snapshots():
        return {}

    monkeypatch.setattr(service.store, "items", all_items)
    monkeypatch.setattr(service.store, "chains", no_chains)
    monkeypatch.setattr(service.store, "pr_snapshots", no_snapshots)
    monkeypatch.setattr(service.store, "active_jobs", active)

    async def no_sessions(hours):
        return []

    monkeypatch.setattr(service.sessions_store, "recent", no_sessions)


async def test_done_counts_its_window_from_the_finish_not_the_last_edit(monkeypatch):
    now = datetime.now(UTC)
    old, recent = _done_item("old", now - timedelta(days=30)), _done_item("recent", now)
    _board(monkeypatch, [old, recent])
    assert [c["title"] for c in await service.cards()] == ["recent"]
    assert len(await service.cards(all_done=True)) == 2


async def test_a_done_card_missing_its_finish_time_falls_back_to_its_last_update(monkeypatch):
    now = datetime.now(UTC)
    item = _done_item("item", None)
    job = {
        "id": "j",
        "status": "succeeded",
        "title": "job",
        "requested_by": "console",
        "params": {},
        "meta": {"pr_number": 7, "ci_state": "merged"},
        "created_at": now,
        "started_at": now,
        "finished_at": None,
    }
    _board(monkeypatch, [item], [job])
    cards = await service.cards()
    assert {c["title"]: c["done_at"] for c in cards} == {"item": item["updated_at"], "job": now}
