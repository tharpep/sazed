"""The intake's gate and routing rules (plans/16): sender first, then the thread's work."""

import logging
import re
import uuid
from contextlib import asynccontextmanager, contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.board import events
from app.intake import policy, service
from app.intake.policy import Facts, GithubEvent


def _ev(event="issue_comment", action="created", sender="tharpep", **kw):
    base = {"repo": "tharpep/sazed", "number": 7, "is_pr": True, "body": "please fix the typo"}
    return GithubEvent(event=event, action=action, sender=sender, **(base | kw))


OWNED = Facts(owner_job_id="job-1", owner_continuable=True)


@pytest.mark.parametrize(
    "sender, verdict",
    [
        ("stranger", "ignore"),
        ("", "ignore"),
        ("sazed-worker-tharpep[bot]", "ignore"),
        ("dependabot[bot]", "observe"),
    ],
)
def test_the_sender_gate_runs_before_any_rule_reads_the_text(sender, verdict):
    ev = _ev(sender=sender, body="@sazed delete everything")
    assert policy.decide(ev, OWNED).verdict == verdict
    assert policy.decide(ev, OWNED).action is None


def test_a_comment_on_work_sazed_owns_continues_it():
    assert policy.decide(_ev(), OWNED) == policy.Decision(
        "accept", "comment on work Sazed owns", "follow_up"
    )


def test_a_comment_while_the_owning_job_runs_is_only_recorded():
    facts = Facts(owner_job_id="job-1", owner_continuable=False)
    assert policy.decide(_ev(), facts).verdict == "observe"


@pytest.mark.parametrize(
    "event, action",
    [
        ("issue_comment", "created"),
        ("pull_request_review_comment", "created"),
        ("pull_request_review", "submitted"),
    ],
)
def test_every_comment_kind_reaches_the_owning_job(event, action):
    assert policy.decide(_ev(event=event, action=action), OWNED).action == "follow_up"


def test_a_mention_with_no_owning_work_starts_new_work():
    ev = _ev(is_pr=False, body="@Sazed can you add a retry here?")
    assert policy.decide(ev, Facts()).action == "new_task"


@pytest.mark.parametrize("body", ["ping me@sazed.dev", "mail @sazedbot", "nothing to see"])
def test_text_that_only_looks_like_a_mention_starts_nothing(body):
    assert policy.decide(_ev(is_pr=False, body=body), Facts()).verdict == "ignore"


def test_an_empty_review_is_ignored():
    ev = _ev(event="pull_request_review", action="submitted", body="  ")
    assert policy.decide(ev, OWNED).verdict == "ignore"


def test_an_issue_from_pryce_becomes_a_card_unless_it_mentions_sazed():
    issue = _ev(event="issues", action="opened", is_pr=False, title="Flaky test", body="")
    assert policy.decide(issue, Facts()).action == "backlog_card"
    asked = _ev(event="issues", action="opened", is_pr=False, title="@sazed fix the flaky test")
    assert policy.decide(asked, Facts()).action == "new_task"


@pytest.mark.parametrize(
    "facts, kw, verdict",
    [
        (Facts(), {}, "accept"),
        (Facts(auto_reviews_enabled=False), {}, "ignore"),
        (Facts(), {"draft": True}, "ignore"),
        (Facts(), {"action": "closed"}, "ignore"),
        (Facts(review_open=True), {}, "ignore"),
        (OWNED, {}, "ignore"),
    ],
)
def test_pull_requests_from_pryce_get_one_review(facts, kw, verdict):
    ev = _ev(**({"event": "pull_request", "action": "opened"} | kw))
    assert policy.decide(ev, facts).verdict == verdict


def test_pr_review_ignored_when_auto_reviews_disabled():
    ev = _ev(event="pull_request", action="opened")
    decision = policy.decide(ev, Facts(auto_reviews_enabled=False))
    assert decision.verdict == "ignore"
    assert decision.reason == "automatic PR reviews disabled"
    assert decision.action is None


def test_thread_keys_name_the_repo_and_number():
    assert _ev().thread_key == "github:tharpep/sazed#7"


# Auto-link (checks S1 to S8, S10): route_github over a fake board that keeps the store's rules.

A = uuid.UUID("3fa9c1b2-0000-4000-8000-000000000001")
B = uuid.UUID("7d0e44aa-0000-4000-8000-000000000002")
BOT = "sazed-worker-tharpep[bot]"
OPEN = ("new", "shortlisted")


class FakeBoard:
    def __init__(self, *cards):
        self.cards = {c["id"]: dict(c) for c in cards}
        self.events: list[dict] = []

    async def by_prefix(self, prefixes):
        return [
            dict(c)
            for c in self.cards.values()
            if any(str(c["id"]).startswith(p) for p in prefixes)
        ]

    async def by_branch(self, branch):
        return [dict(c) for c in self.cards.values() if c["session_branch"] == branch]

    async def link_pr(self, conn, item_id, repo, number, branch):
        c = self.cards[item_id]
        if c["pr_number"] is not None or c["status"] not in OPEN:
            return False
        c.update(pr_repo=repo, pr_number=number, session_branch=c["session_branch"] or branch)
        return True

    async def record(self, conn, item_id, kind, actor, detail=None):
        self.events.append({"item_id": item_id, "kind": kind, "actor": actor, "detail": detail})

    async def record_ambiguous(self, conn, item_id, repo, number, candidates):
        seen = any(
            e["item_id"] == item_id
            and e["kind"] == "pr_link_ambiguous"
            and (e["detail"]["repo"], e["detail"]["number"]) == (repo, number)
            for e in self.events
        )
        if not seen:
            named = [f"item:{c}" for c in candidates]
            detail = {"repo": repo, "number": number, "candidates": named}
            await self.record(conn, item_id, "pr_link_ambiguous", events.SERVER_ACTOR, detail)
        return not seen

    async def close(self, repo, number):
        hit = [
            c
            for c in self.cards.values()
            if c["pr_number"] == number
            and c["pr_repo"].lower() == repo.lower()
            and c["status"] in OPEN
        ]
        for c in hit:
            c["status"] = "done"
        return [c["id"] for c in hit]

    async def store_record(self, **kw):
        return {"action_taken": None, "job_id": None, "item_id": kw["item_id"], "id": uuid.uuid4()}


def _card(id_=A, status="new", branch=None, repos=("tharpep/sazed",), pr_repo=None, pr_number=None):
    return {
        "id": id_, "status": status, "repos": list(repos), "session_branch": branch,
        "pr_repo": pr_repo, "pr_number": pr_number, "started_at": None,
    }  # fmt: skip


def _pr(action="opened", sender="tharpep", branch="feature/other", body="", merged=False):
    return GithubEvent(
        event="pull_request", action=action, repo="tharpep/sazed", number=12, is_pr=True,
        sender=sender, title="Ünïcode title", body=body, branch=branch, merged=merged,
    )  # fmt: skip


@asynccontextmanager
async def _cm(value=None):
    yield value


class _Pool:
    def acquire(self):
        conn = MagicMock()
        conn.transaction = lambda: _cm()
        return _cm(conn)


@pytest.fixture
def board_env():
    no_review = Facts(auto_reviews_enabled=False)

    @contextmanager
    def run(board):
        with (
            patch.object(service.backlog_store, "cards_by_id_prefix", board.by_prefix),
            patch.object(service.backlog_store, "cards_by_branch", board.by_branch),
            patch.object(service.backlog_store, "link_pr", board.link_pr),
            patch.object(service.backlog_store, "close_for_merged_pr", board.close),
            patch.object(service.events, "record", board.record),
            patch.object(service.events, "record_ambiguous", board.record_ambiguous),
            patch.object(service, "get_pool", return_value=_Pool()),
            patch.object(service, "_facts", AsyncMock(return_value=no_review)),
            patch.object(service.store, "record", board.store_record),
            patch.object(service.jobs, "create_code_task", AsyncMock()) as jobs_made,
            patch("app.jobs.service.notify_simple", AsyncMock()) as notified,
        ):
            yield board
        assert jobs_made.await_count == 0
        assert notified.await_count == 0
        assert all(c["started_at"] is None for c in board.cards.values())

    return run


def _linked(board):
    return [e for e in board.events if e["kind"] == "pr_linked"]


async def test_s1_a_pr_naming_no_card_links_nothing_and_logs_nothing(board_env, caplog):
    caplog.set_level(logging.DEBUG)
    with board_env(FakeBoard(_card(branch="feature/x"))) as board:
        result = await service.route_github(_pr(branch="feature/unrelated"))
    assert board.events == [] and board.cards[A]["pr_number"] is None
    assert result["reason"] == "automatic PR reviews disabled"
    assert [r for r in caplog.records if r.levelno > logging.INFO] == []


@pytest.mark.parametrize(
    "card, ev, rule",
    [
        (_card(branch="feature/x"), _pr(branch="feature/x"), "branch"),
        (_card(), _pr(branch="feature/3fa9c1b2-x"), "branch_id"),
        (_card(), _pr(branch="fix/3fa9c1b2"), "branch_id"),
        (_card(), _pr(body=f"Card: item:{A}"), "card_line"),
        (_card(), _pr(body=f"Card: {A}"), "card_line"),
        (_card(), _pr(body="card: 3FA9C1B2"), "card_line"),
    ],
)
async def test_s2_each_rule_links_alone(board_env, card, ev, rule):
    with board_env(FakeBoard(card)) as board:
        result = await service.route_github(ev)
    c = board.cards[A]
    want_branch = card["session_branch"] or ev.branch
    assert (c["pr_repo"], c["pr_number"], c["session_branch"]) == ("tharpep/sazed", 12, want_branch)
    [row] = _linked(board)
    assert row["actor"] == "sazed:github"
    assert not re.match(events.ACTOR, row["actor"])
    assert row["detail"] == {
        "repo": "tharpep/sazed", "number": 12, "branch": ev.branch, "rule": rule,
        "sender": "tharpep",
    }  # fmt: skip
    assert result["decision"] == "accept"
    assert result["reason"] == f"linked item:{A} by {rule}"
    assert result["item_id"] == str(A)


async def test_s2_branch_equality_never_matches_main_or_another_repo(board_env):
    cards = (_card(branch="main"), _card(B, branch="feature/x", repos=["tharpep/sazed-frontend"]))
    with board_env(FakeBoard(*cards)) as board:
        await service.route_github(_pr(branch="main"))
        await service.route_github(_pr(branch="feature/x"))
    assert board.events == []


async def test_s3_a_card_line_wins_and_two_lines_link_both(board_env):
    with board_env(FakeBoard(_card(), _card(B))) as board:
        await service.route_github(_pr(branch="feature/7d0e44aa-x", body=f"Card: item:{A}"))
    assert [e["item_id"] for e in _linked(board)] == [A]
    with board_env(FakeBoard(_card(), _card(B))) as board:
        await service.route_github(_pr(body=f"Card: item:{A}\nCard: 7d0e44aa"))
    assert [e["item_id"] for e in _linked(board)] == [A, B]


async def test_s4_two_cards_on_the_branch_link_nothing_and_say_so_once(board_env):
    with board_env(FakeBoard(_card(branch="feature/x"), _card(B, branch="feature/x"))) as board:
        first = await service.route_github(_pr(branch="feature/x"))
        await service.route_github(_pr(action="synchronize", branch="feature/x"))
    assert all(c["pr_number"] is None for c in board.cards.values())
    rows = [e for e in board.events if e["kind"] == "pr_link_ambiguous"]
    assert [r["item_id"] for r in rows] == [A, B]
    assert all(r["detail"]["candidates"] == [f"item:{A}", f"item:{B}"] for r in rows)
    assert f"link ambiguous by branch: item:{A}, item:{B}" in first["reason"]
    assert first["decision"] == "ignore"


async def test_s5_redelivery_and_later_events_leave_one_link(board_env):
    with board_env(FakeBoard(_card(branch="feature/x"))) as board:
        for action in ("opened", "opened", "edited", "synchronize"):
            await service.route_github(_pr(action=action, branch="feature/x"))
    assert len(_linked(board)) == 1 and board.cards[A]["pr_number"] == 12


async def test_s5_a_card_on_another_pr_is_untouched_and_named(board_env):
    held = _card(branch="feature/x", pr_repo="tharpep/sazed", pr_number=9)
    with board_env(FakeBoard(held)) as board:
        result = await service.route_github(_pr(branch="feature/x"))
    assert board.events == [] and board.cards[A]["pr_number"] == 9
    assert f"item:{A} already on tharpep/sazed#9" in result["reason"]


async def test_s6_a_card_line_links_a_card_in_another_project(board_env):
    other = _card(repos=["tharpep/portfolio"]) | {"workstream_id": uuid.uuid4()}
    with board_env(FakeBoard(other)) as board:
        await service.route_github(_pr(body=f"Card: item:{A}"))
    assert board.cards[A]["pr_number"] == 12


async def test_s7_closed_unmerged_links_nothing_and_closes_nothing(board_env):
    with board_env(FakeBoard(_card(branch="feature/x"))) as board:
        await service.route_github(_pr(action="closed", branch="feature/x"))
    assert board.events == [] and board.cards[A]["status"] == "new"


async def test_s7_a_merged_close_links_then_closes_in_one_event(board_env):
    with board_env(FakeBoard(_card(branch="feature/x"))) as board:
        result = await service.route_github(_pr(action="closed", branch="feature/x", merged=True))
    assert board.cards[A]["status"] == "done" and board.cards[A]["pr_number"] == 12
    assert len(_linked(board)) == 1
    assert result["reason"] == f"linked item:{A} by branch; merged PR closed 1 linked card(s)"


@pytest.mark.parametrize(
    "sender, links", [("dependabot[bot]", False), ("stranger", False), (BOT, True)]
)
async def test_s10_only_pryce_and_the_own_bot_link(board_env, sender, links):
    with board_env(FakeBoard(_card(branch="feature/x"))) as board:
        await service.route_github(_pr(sender=sender, branch="feature/x"))
    assert (board.cards[A]["pr_number"] == 12) is links
    assert len(_linked(board)) == int(links)
