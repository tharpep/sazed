"""PR state on cards (board visibility slice 2): the review verdict, the merge call for a
session-linked card, a done card's PR, and the CI watcher's snapshot pass."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.encoders import jsonable_encoder

from app.board import prs, stages
from app.board import service as board
from app.jobs import ci_watch

NOW = datetime(2026, 11, 19, 20, 55, tzinfo=UTC)

# The bot's review of tharpep/sazed#203, cut to the parts the parser reads.
REVIEW_203 = """## Verdict
**Approve** · Confidence: high · Review effort: 1 (about 5 min)
This PR gates automatic pull request reviews behind a new `enable_pr_auto_reviews` setting.

## What changed
| File | Change | Risk |
|---|---|---|
| `app/config.py` | Adds `enable_pr_auto_reviews: bool = False` | Low |

<details><summary>Review data</summary>

```json
{"verdict": "Approve", "confidence": "high", "effort": 1, "findings": []}
```
</details>"""


def _review(verdict_line, json_verdict):
    return REVIEW_203.replace("**Approve**", f"**{verdict_line}**").replace(
        '"verdict": "Approve"', f'"verdict": "{json_verdict}"'
    )


@pytest.mark.parametrize(
    "text, verdict",
    [
        (REVIEW_203, "approve"),
        (_review("Approve with nits", "Approve with nits"), "approve_with_nits"),
        (_review("Request changes", "Request changes"), "request_changes"),
        (_review("Approve", "request_changes"), "request_changes"),
        (REVIEW_203.split("<details>")[0], "approve"),
        (_review("Request changes", "...").split("<details>")[0], "request_changes"),
    ],
    ids=["real", "nits", "changes", "json-wins", "heading-only", "heading-changes"],
)
def test_the_verdict_reads_from_real_review_text(text, verdict):
    assert prs.parse_verdict(text) == verdict


@pytest.mark.parametrize(
    "text",
    [
        None,
        42,
        "",
        "LGTM",
        "## Verdict\nShip it",
        "```json\n{not json\n```",
        "```json\n" + "[" * 100_000 + "\n```",
        '```json\n{"verdict": ["Approve"]}\n```',
        _review("Maybe", "..."),
    ],
    ids=["none", "int", "empty", "prose", "unknown", "bad-json", "deep-json", "list", "maybe"],
)
def test_anything_else_is_no_verdict_and_never_raises(text):
    assert prs.parse_verdict(text) is None


QUOTED_APPROVE = '```json\n{"verdict": "approve"}\n```\n'


@pytest.mark.parametrize(
    "text, verdict",
    [
        (
            _review("Request changes", "x")
            .replace('{"verdict": "x"', '{"verdict": "x",,')
            .replace("## What changed", "## What changed\n" + QUOTED_APPROVE),
            "request_changes",
        ),
        (
            _review("Request changes", "...").replace("## What changed", QUOTED_APPROVE),
            "request_changes",
        ),
        (_review("Request changes", "Approve"), "request_changes"),
        (_review("Approve with nits", "Approve"), "approve_with_nits"),
        (_review("Approve", "Approve with nits"), "approve_with_nits"),
    ],
    ids=[
        "bad-last-json-falls-to-heading",
        "earlier-quoted-json-ignored",
        "heading-stricter-than-json",
        "nits-heading-over-json-approve",
        "nits-json-over-heading-approve",
    ],
)
def test_quoted_text_can_only_make_the_verdict_stricter(text, verdict):
    assert prs.parse_verdict(text) == verdict


def test_the_last_heading_counts_when_there_is_no_json():
    text = "## Verdict\n**Approve with nits**\n\nquoted:\n## Verdict\n**Approve**"
    assert prs.parse_verdict(text) == "approve"


def test_no_stored_review_is_no_review_not_approve():
    assert prs.review(None) is None
    assert prs.review({"agent": "agy", "result": "the worker died", "meta": {}}) is None
    job = {"agent": "agy", "result": REVIEW_203, "meta": {"pr_review_url": "https://r"}}
    assert prs.review(job) == {"verdict": "approve", "source": "agy", "url": "https://r"}


def _pr(state="open", checks="passing", verdict=None, fetched=NOW):
    return {
        "state": state,
        "checks": {"state": checks, "passed": 2, "total": 2, "runs": []} if checks else None,
        "review": {"verdict": verdict, "source": "agy", "url": "u"} if verdict else None,
        "fetched_at": fetched,
    }


def _linked(status="shortlisted"):
    return {"status": status, "rank": None, "workstream_id": None, "session_label": "cc-7",
            "session_branch": "feature/x", "pr_number": 12}  # fmt: skip


@pytest.mark.parametrize(
    "pr, stage, why",
    [
        (_pr(), "needs_me", "Ready to merge"),
        (_pr(verdict="approve_with_nits"), "needs_me", "Ready to merge"),
        (_pr(verdict="approve"), "needs_me", "Ready to merge"),
        (_pr(verdict="request_changes"), "doing", "Review asked for changes"),
        (_pr(state="draft"), "doing", "Draft PR"),
        (_pr(checks="failing"), "doing", "Checks failing"),
        (_pr(checks="running"), "doing", "Checks running"),
        (_pr(checks="none"), "doing", "No checks reported yet"),
        (_pr(state="merged"), "doing", "PR merged"),
        (_pr(state="closed"), "doing", "PR closed without merging"),
        (_pr(checks=None, fetched=None), "doing", "Being built in a terminal session"),
        (None, "doing", "Being built in a terminal session"),
    ],
)
def test_the_merge_call_for_a_session_linked_card(pr, stage, why):
    assert stages.item_stage(_linked(), None, pr=pr) == (stage, why)


def test_a_done_linked_card_stays_done_whatever_its_pr():
    assert stages.item_stage(_linked("done"), None, pr=_pr())[0] == "done"


def _item(status, **over):
    return {
        "id": uuid.uuid4(), "type": "planned", "status": status, "title": "Card",
        "repos": ["tharpep/sazed"], "effort": None, "impact": None, "rank": None,
        "job_id": None, "workstream_id": None, "plan_id": None, "plan_section": None,
        "plan_version": None, "created_at": NOW, "updated_at": NOW, "decided_at": NOW,
        "session_label": None, "session_branch": None, "pr_repo": None, "pr_number": None,
    } | over  # fmt: skip


def _job(**meta):
    return {
        "id": uuid.uuid4(), "status": "succeeded", "requested_by": "console",
        "params": {"repo": "tharpep/sazed-frontend"}, "created_at": NOW, "finished_at": NOW,
        "meta": {"pr_number": 83, "pr_url": "https://github.com/tharpep/sazed-frontend/pull/83",
                 "ci_state": "merged"} | meta,
    }  # fmt: skip


SNAP = {
    "state": "merged",
    "head_sha": "abc",
    "merged_at": datetime(2026, 11, 19, 20, 53, 40, tzinfo=UTC),
    "additions": 1148,
    "deletions": 105,
    "checks": {
        "state": "passing",
        "passed": 2,
        "total": 2,
        "runs": [
            {"name": "check", "conclusion": "success"},
            {"name": "tests", "conclusion": "success"},
        ],
    },  # fmt: skip
    "review_verdict": "approve",
    "review_source": "agy",
    "review_url": "https://github.com/tharpep/sazed-frontend/pull/83#pullrequestreview-1",
    "fetched_at": NOW,
    "attempted_at": NOW,
    "failures": 0,
}


def test_a_done_card_keeps_its_jobs_pr_with_the_contract_shape():
    snaps = {("tharpep/sazed-frontend", 83): SNAP}
    card = board._item_card(_item("done"), [_job()], snaps)
    assert card["stage"] == "done"
    assert card["pr_number"] is None  # the old field keeps its old meaning (live jobs only)
    assert jsonable_encoder(card["pr"]) == {
        "repo": "tharpep/sazed-frontend",
        "number": 83,
        "url": "https://github.com/tharpep/sazed-frontend/pull/83",
        "state": "merged",
        "checks": {
            "state": "passing",
            "passed": 2,
            "total": 2,
            "runs": [
                {"name": "check", "conclusion": "success"},
                {"name": "tests", "conclusion": "success"},
            ],
        },  # fmt: skip
        "review": {
            "verdict": "approve",
            "source": "agy",
            "url": "https://github.com/tharpep/sazed-frontend/pull/83#pullrequestreview-1",
        },
        "additions": 1148,
        "deletions": 105,
        "merged_at": "2026-11-19T20:53:40+00:00",
        "fetched_at": "2026-11-19T20:55:00+00:00",
    }


def test_a_reopened_card_drops_its_old_jobs_pr():
    assert board._item_card(_item("new"), [_job(ci_state="closed")], {})["pr"] is None


def test_a_pr_never_fetched_says_what_sazed_knows():
    item = _item("shortlisted", session_label="cc-7", pr_repo="tharpep/sazed", pr_number=12)
    card = board._item_card(item, [], {})
    assert card["stage"] == "doing" and card["next"] == "Being built in a terminal session"
    assert card["session_link"]["pr_number"] == 12
    pr = card["pr"]
    assert (pr["state"], pr["checks"], pr["review"], pr["fetched_at"]) == ("open", None, None, None)
    assert pr["url"] == "https://github.com/tharpep/sazed/pull/12"


def test_a_card_without_a_pr_has_a_null_pr():
    assert board._item_card(_item("new"), [], {})["pr"] is None


def test_a_green_linked_pr_joins_needs_you():
    item = _item("shortlisted", session_label="cc-7", pr_repo="Tharpep/Sazed", pr_number=12)
    snap = SNAP | {"state": "open", "merged_at": None, "review_verdict": None}
    card = board._item_card(item, [], {("tharpep/sazed", 12): snap})
    assert (card["stage"], card["next"], card["actions"]) == ("needs_me", "Ready to merge", [])
    assert card["pr"]["review"] is None


def _snap(state="open", age=timedelta(0)):
    return {"state": state, "fetched_at": NOW - age, "head_sha": "abc", "checks": None,
            "attempted_at": NOW - age, "failures": 0}  # fmt: skip


def _failed(failures, age, fetched=None):
    return {"state": None, "fetched_at": fetched, "head_sha": None, "checks": None,
            "attempted_at": NOW - age, "failures": failures}  # fmt: skip


def _due(items, snaps, jobs=(), chains=None):
    return [
        (r["repo"], r["number"])
        for r, _ in prs.due(items, chains or {}, list(jobs), snaps, NOW, timedelta(days=14))
    ]


def _session_item(number, status="shortlisted", decided=None):
    return _item(status, pr_repo="tharpep/sazed", pr_number=number, decided_at=decided)


def test_the_pass_skips_young_snapshots_and_never_refetches_a_merged_pr():
    items = [_session_item(1), _session_item(2), _session_item(3), _session_item(4)]
    snaps = {
        ("tharpep/sazed", 2): _snap(age=timedelta(minutes=1)),
        ("tharpep/sazed", 3): _snap("merged", age=timedelta(days=1)),
        ("tharpep/sazed", 4): _snap(age=timedelta(minutes=10)),
    }
    assert _due(items, snaps) == [("tharpep/sazed", 1), ("tharpep/sazed", 4)]


def test_a_done_cards_pr_is_fetched_once_after_it_turned_done():
    done_at = NOW - timedelta(hours=1)
    item = _session_item(5, "done", done_at)
    before = {("tharpep/sazed", 5): _snap(age=timedelta(hours=2))}
    after = {("tharpep/sazed", 5): _snap(age=timedelta(minutes=30))}
    assert _due([item], before) == [("tharpep/sazed", 5)]
    assert _due([item], after) == []
    old = _session_item(6, "done", NOW - timedelta(days=30))
    assert _due([old], {}) == []


def test_a_running_jobs_pr_is_due_too():
    job = _job(ci_state=None) | {"status": "paused"}
    assert _due([], {}, jobs=[job]) == [("tharpep/sazed-frontend", 83)]


def test_a_failing_ref_backs_off_and_sorts_behind_healthy_ones():
    items = [_session_item(n) for n in (1, 2, 3, 4)]
    snaps = {
        ("tharpep/sazed", 1): _failed(1, timedelta(minutes=1)),
        ("tharpep/sazed", 2): _failed(1, timedelta(minutes=7)),
        ("tharpep/sazed", 3): _failed(3, timedelta(minutes=20)),
        ("tharpep/sazed", 4): _snap(age=timedelta(minutes=10)),
    }
    assert _due(items, snaps) == [("tharpep/sazed", 4), ("tharpep/sazed", 2)]
    assert prs.backoff(1) == prs.SNAPSHOT_AGE and prs.backoff(9) == prs.SNAPSHOT_AGE * 32


@pytest.fixture
def gateway(monkeypatch):
    from app.backlog import store as backlog_store

    calls = {"pr": None, "ci": None, "ci_asked": 0, "saved": [], "failed": [], "closed": []}

    async def fake_pr(repo, number):
        return calls["pr"]

    async def fake_ci(repo, sha):
        calls["ci_asked"] += 1
        return calls["ci"]

    async def no_review(repo, number):
        return None

    async def save(key, snap):
        calls["saved"].append((key, snap))

    async def failed(key):
        calls["failed"].append(key)

    async def close(repo, number):
        calls["closed"].append((repo, number))
        return []

    monkeypatch.setattr(ci_watch, "pull_request", fake_pr)
    monkeypatch.setattr(ci_watch, "_ci", fake_ci)
    monkeypatch.setattr(ci_watch.board_store, "latest_review", no_review)
    monkeypatch.setattr(ci_watch.board_store, "save_pr_snapshot", save)
    monkeypatch.setattr(ci_watch.board_store, "record_pr_failure", failed)
    monkeypatch.setattr(backlog_store, "close_for_merged_pr", close)
    return calls


def _fake_board(monkeypatch, items, snaps):
    async def rows():
        return items

    async def none():
        return {}

    async def no_jobs():
        return []

    async def snapshots():
        return dict(snaps)

    monkeypatch.setattr(ci_watch.board_store, "items", rows)
    monkeypatch.setattr(ci_watch.board_store, "chains", none)
    monkeypatch.setattr(ci_watch.board_store, "active_jobs", no_jobs)
    monkeypatch.setattr(ci_watch.board_store, "pr_snapshots", snapshots)


REF = {"repo": "tharpep/Sazed", "number": 12, "known": "open"}
NEW_PR = {"state": "open", "draft": False, "merged": False, "merged_at": None,
          "head_sha": "def", "additions": 10, "deletions": 2, "changed_files": 1}  # fmt: skip


async def test_a_failing_gateway_leaves_the_old_snapshot(gateway):
    await ci_watch._snapshot(REF, _snap())
    gateway["pr"] = NEW_PR
    await ci_watch._snapshot(REF, _snap())
    assert gateway["ci_asked"] == 1
    assert gateway["saved"] == []


async def test_a_pass_with_a_failing_gateway_raises_nothing_and_records_it(gateway, monkeypatch):
    async def boom(repo, number):
        raise ValueError("gateway sent HTML")

    _fake_board(monkeypatch, [_session_item(1), _session_item(2)], {})
    monkeypatch.setattr(ci_watch, "pull_request", boom)
    await ci_watch.snapshot_prs()
    assert gateway["saved"] == []
    assert gateway["failed"] == [("tharpep/sazed", 1), ("tharpep/sazed", 2)]


async def test_ten_broken_refs_cannot_starve_a_healthy_one(gateway, monkeypatch):
    items = [_session_item(n) for n in range(1, 12)]
    snaps: dict = {}
    _fake_board(monkeypatch, items, snaps)

    async def fake_pr(repo, number):
        return NEW_PR if number == 11 else None

    async def failed(key):
        old = snaps.get(key) or _failed(0, timedelta(0))
        snaps[key] = old | {"failures": old["failures"] + 1, "attempted_at": datetime.now(UTC)}

    async def save(key, snap):
        gateway["saved"].append((key, snap))
        snaps[key] = snap | {"fetched_at": datetime.now(UTC), "attempted_at": datetime.now(UTC),
                             "failures": 0}  # fmt: skip

    gateway["ci"] = {"state": "success", "runs": []}
    monkeypatch.setattr(ci_watch, "pull_request", fake_pr)
    monkeypatch.setattr(ci_watch.board_store, "record_pr_failure", failed)
    monkeypatch.setattr(ci_watch.board_store, "save_pr_snapshot", save)
    for _ in range(2):
        await ci_watch.snapshot_prs()
    assert [k for k, _ in gateway["saved"]] == [("tharpep/sazed", 11)]


async def test_a_hanging_gateway_cannot_hold_the_tick_past_its_budget(gateway, monkeypatch):
    import asyncio
    import time

    seen = {"resumed": False}

    async def hang(repo, number):
        await asyncio.sleep(3600)

    async def nothing_awaiting():
        return []

    async def resume():
        seen["resumed"] = True
        return 0

    _fake_board(monkeypatch, [_session_item(1)], {})
    monkeypatch.setattr(ci_watch, "pull_request", hang)
    monkeypatch.setattr(ci_watch, "SNAPSHOT_BUDGET_S", 0.2)
    monkeypatch.setattr(ci_watch.store, "awaiting_ci", nothing_awaiting)
    monkeypatch.setattr(ci_watch.service, "resume_rate_limited", resume)
    started = time.monotonic()
    await ci_watch._tick(1, None)
    assert time.monotonic() - started < 2
    assert seen["resumed"]


async def test_a_pr_seen_merged_closes_its_linked_card_once(gateway):
    gateway["pr"] = NEW_PR | {"state": "closed", "merged": True}
    gateway["ci"] = {"state": "success", "runs": []}
    await ci_watch._snapshot(REF, _snap())
    assert gateway["closed"] == [("tharpep/Sazed", 12)]
    await ci_watch._snapshot(REF, _snap("merged"))
    assert len(gateway["closed"]) == 1


async def test_a_pr_closed_unmerged_leaves_its_card(gateway):
    gateway["pr"] = NEW_PR | {"state": "closed", "merged": False}
    gateway["ci"] = {"state": "success", "runs": []}
    await ci_watch._snapshot(REF, _snap())
    assert gateway["saved"][0][1]["state"] == "closed"
    assert gateway["closed"] == []


async def test_a_merged_pr_keeps_recorded_checks_without_asking(gateway):
    gateway["pr"] = NEW_PR | {"state": "closed", "merged": True}
    passing = {"state": "passing", "passed": 1, "total": 1, "runs": []}
    await ci_watch._snapshot(REF, _snap() | {"checks": passing})
    assert gateway["ci_asked"] == 0
    assert gateway["saved"][0][1]["checks"] == passing


async def test_a_failed_ci_call_never_stops_a_merged_state(gateway):
    gateway["pr"] = NEW_PR | {"state": "closed", "merged": True}
    assert await ci_watch._snapshot(REF, None)
    [(_, snap)] = gateway["saved"]
    assert (snap["state"], snap["checks"], gateway["ci_asked"]) == ("merged", None, 1)


async def test_an_old_gateway_still_gives_state_and_merged_time(gateway):
    gateway["pr"] = {"state": "closed", "draft": False, "merged": True,
                     "merged_at": "2026-11-19T20:53:40Z", "mergeable": None}  # fmt: skip
    await ci_watch._snapshot(REF, None)
    [(key, snap)] = gateway["saved"]
    assert key == ("tharpep/sazed", 12)
    assert snap["state"] == "merged"
    assert snap["merged_at"] == datetime(2026, 11, 19, 20, 53, 40, tzinfo=UTC)
    assert snap["checks"] == {"state": "none", "passed": 0, "total": 0, "runs": []}
    assert (snap["additions"], snap["deletions"], snap["head_sha"]) == (None, None, None)
    assert gateway["ci_asked"] == 0


async def test_a_new_gateway_gives_checks_and_diff_size(gateway):
    gateway["pr"] = NEW_PR
    gateway["ci"] = {"state": "failure", "runs": [
        {"name": "lint", "status": "completed", "conclusion": "success"},
        {"name": "tests", "status": "completed", "conclusion": "failure"},
    ]}  # fmt: skip
    await ci_watch._snapshot(REF, None)
    [(_, snap)] = gateway["saved"]
    assert snap["checks"] == {
        "state": "failing", "passed": 1, "total": 2,
        "runs": [{"name": "lint", "conclusion": "success"},
                 {"name": "tests", "conclusion": "failure"}],
    }  # fmt: skip
    assert (snap["state"], snap["additions"], snap["deletions"]) == ("open", 10, 2)


async def test_passing_checks_on_an_unchanged_head_are_not_asked_again(gateway):
    gateway["pr"] = NEW_PR
    passing = {"state": "passing", "passed": 1, "total": 1, "runs": []}
    await ci_watch._snapshot(REF, _snap() | {"head_sha": "def", "checks": passing})
    assert gateway["ci_asked"] == 0
    assert gateway["saved"][0][1]["checks"] == passing
