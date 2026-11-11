"""A terminal session links itself to a card: it shows as doing, and its PR merging closes it."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from app.backlog import store
from app.board import stages
from app.intake import policy, service
from app.routers import backlog as router


def _item(status="new", **link):
    base = {"status": status, "rank": None, "workstream_id": None}
    return base | {"session_label": None, "session_branch": None, "pr_number": None} | link


@pytest.mark.parametrize(
    "link",
    [{"session_label": "cc-7"}, {"session_branch": "feature/x"}, {"pr_number": 12}],
)
def test_a_linked_card_is_doing(link):
    assert stages.item_stage(_item(**link), None)[0] == "doing"
    assert stages.item_stage(_item("shortlisted", **link), None)[0] == "doing"


def test_an_unlinked_card_keeps_its_old_stage():
    assert stages.item_stage(_item(), None)[0] == "inbox"
    assert stages.item_stage(_item(rank=2), None)[0] == "next"


def test_a_done_card_stays_done_whatever_its_link():
    assert stages.item_stage(_item("done", pr_number=12), None)[0] == "done"


def test_a_card_with_a_job_keeps_the_job_stage():
    job = {"status": "needs_approval", "meta": {}}
    assert stages.item_stage(_item("approved", pr_number=12), job)[0] == "needs_me"


@pytest.mark.parametrize(
    "ref",
    ["https://github.com/tharpep/sazed/pull/12", "tharpep/sazed#12", "tharpep/sazed/pull/12/"],
)
def test_a_pr_is_named_by_url_or_short_form(ref):
    cols = router.SessionLink(pr=ref).columns()
    assert (cols["pr_repo"], cols["pr_number"]) == ("tharpep/sazed", 12)


@pytest.mark.parametrize("bad", [{}, {"pr": "sazed#12"}, {"pr": "https://example.com/a/b/pull/1"}])
def test_a_link_needs_something_and_a_real_pr(bad):
    with pytest.raises(ValidationError):
        router.SessionLink(**bad)


async def _patch(body):
    with patch.object(router.store, "update_item", AsyncMock(return_value={"id": "x"})) as upd:
        await router.update_item(uuid.uuid4(), body)
    return upd.call_args.kwargs


async def test_patch_sets_the_link_columns():
    link = router.SessionLink(session="cc-7", branch="feature/x", pr="tharpep/sazed#12")
    assert await _patch(router.ItemUpdate(link=link)) == {
        "session_label": "cc-7",
        "session_branch": "feature/x",
        "pr_repo": "tharpep/sazed",
        "pr_number": 12,
    }


async def test_patch_with_a_null_link_clears_every_column():
    assert await _patch(router.ItemUpdate(link=None)) == {
        "session_label": None,
        "session_branch": None,
        "pr_repo": None,
        "pr_number": None,
    }


def _event(action="closed", merged=True):
    return policy.GithubEvent(
        event="pull_request",
        action=action,
        repo="tharpep/sazed",
        number=12,
        is_pr=True,
        sender="tharpep",
        merged=merged,
    )


async def _route(ev, closes):
    row = {"action_taken": None, "job_id": None, "item_id": None, "id": uuid.uuid4()}
    with (
        patch.object(service.backlog_store, "close_for_merged_pr", closes),
        patch.object(service, "_facts", AsyncMock(return_value=policy.Facts())),
        patch.object(service.store, "record", AsyncMock(return_value=row)),
    ):
        await service.route_github(ev)


async def test_a_merged_pr_event_closes_the_cards_linked_to_it():
    closes = AsyncMock(return_value=[uuid.uuid4()])
    await _route(_event(), closes)
    closes.assert_awaited_once_with("tharpep/sazed", 12)


@pytest.mark.parametrize("ev", [_event(merged=False), _event(action="synchronize")])
async def test_a_pr_closed_unmerged_or_still_open_closes_nothing(ev):
    closes = AsyncMock(return_value=[])
    await _route(ev, closes)
    closes.assert_not_awaited()


async def test_closing_only_touches_open_cards_without_a_job():
    pool = MagicMock()
    pool.fetch = AsyncMock(return_value=[{"id": uuid.uuid4()}])
    with (
        patch.object(store, "get_pool", return_value=pool),
        patch.object(store, "normalize_ranks", AsyncMock()),
    ):
        ids = await store.close_for_merged_pr("tharpep/sazed", 12)
    sql, repo, number, statuses = pool.fetch.call_args.args
    assert len(ids) == 1
    assert "job_id IS NULL" in sql
    assert (repo, number, statuses) == ("tharpep/sazed", 12, ["new", "shortlisted"])


def test_the_board_card_shows_the_link_and_the_doing_stage():
    from datetime import UTC, datetime

    from app.board import service as board

    now = datetime.now(UTC)
    item = {
        "id": uuid.uuid4(), "type": "planned", "status": "shortlisted", "title": "Card",
        "repos": ["tharpep/sazed"], "effort": None, "impact": None, "rank": None,
        "job_id": None, "workstream_id": None, "plan_id": None, "plan_section": None,
        "plan_version": None, "created_at": now, "updated_at": now,
        "session_label": "cc-7", "session_branch": "feature/x",
        "pr_repo": "tharpep/sazed", "pr_number": 12,
    }  # fmt: skip
    card = board._item_card(item, [])
    assert card["stage"] == "doing"
    assert card["session_link"] == {
        "session": "cc-7", "branch": "feature/x", "pr_repo": "tharpep/sazed", "pr_number": 12
    }  # fmt: skip
