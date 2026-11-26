"""The PR-to-card matching rules (autolink), pure, plus the suggested branch on POST /backlog."""

import random
import re
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from app.board import autolink, events
from app.routers import backlog as router

A = uuid.UUID("3fa9c1b2-0000-4000-8000-000000000001")
B = uuid.UUID("7d0e44aa-0000-4000-8000-000000000002")


def _card(id_=A, status="new", repos=(), branch=None, pr_repo=None, pr_number=None):
    return {
        "id": id_,
        "status": status,
        "repos": list(repos),
        "session_branch": branch,
        "pr_repo": pr_repo,
        "pr_number": pr_number,
    }


def _choose(refs=(), by_ref=(), short=None, by_short=(), by_branch=(), repo="tharpep/sazed"):
    return autolink.choose(repo, 12, list(refs), list(by_ref), short, list(by_short), by_branch)


@pytest.mark.parametrize(
    "body",
    [
        f"Card: item:{A}",
        f"card: {A}",
        "CARD:3fa9c1b2",
        f"Some text\n\n  Card:   item:{str(A).upper()}  \nmore",
    ],
)
def test_a_card_line_names_the_card_in_all_three_forms(body):
    assert autolink.card_refs(body) in ([str(A)], ["3fa9c1b2"])


@pytest.mark.parametrize(
    "body",
    [
        f"see Card: item:{A}",
        f"Card: item:{A} and more",
        "Card: 3fa9c1b",
        "Card: 3fa9c1b2x",
        f"Cards: {A}",
        "",
    ],
)
def test_text_that_only_looks_like_a_card_line_names_nothing(body):
    assert autolink.card_refs(body) == []


@pytest.mark.parametrize(
    "body",
    [
        "Summary\r\nCard: 3fa9c1b2\r\nmore\r\n",
        "Summary\nother\r\nCard: 3fa9c1b2\n",
        "Summary\r\nCard: 3fa9c1b2  \r\n",
        "Card: 3fa9c1b2\r",
    ],
)
def test_a_card_line_in_a_crlf_body_names_the_card(body):
    assert autolink.card_refs(body) == ["3fa9c1b2"]


def test_card_lines_are_deduped_and_capped():
    lines = [f"Card: {i:08x}" for i in range(15)] + ["Card: 00000000"]
    refs = autolink.card_refs("\n".join(lines))
    assert len(refs) == autolink.MAX_CARD_LINES
    assert refs[0] == "00000000" and len(set(refs)) == len(refs)


def test_a_card_line_past_the_body_limit_is_not_read():
    assert autolink.card_refs("x" * autolink.BODY_LIMIT + "\nCard: 3fa9c1b2") == []


@pytest.mark.parametrize(
    "branch, short",
    [
        ("feature/3fa9c1b2-x", "3fa9c1b2"),
        ("fix/3fa9c1b2", "3fa9c1b2"),
        ("3FA9C1B2-upper", "3fa9c1b2"),
        ("feature/x-3fa9c1b2", None),
        ("feature/3fa9c1b2x", None),
        ("3fa9c1b2-x/later", None),
        ("main", None),
        (None, None),
        ("feature/3fa9c1b2-" + "x" * 300, None),
    ],
)
def test_the_id_in_a_branch_starts_its_last_segment(branch, short):
    assert autolink.branch_id(branch) == short


def test_branch_equality_links_one_open_card_in_the_repo():
    out = _choose(by_branch=[_card(repos=["sazed"], branch="feature/x")])
    assert (out.rule, out.link) == ("branch", [A])


def test_branch_equality_skips_a_card_for_another_repo():
    out = _choose(by_branch=[_card(repos=["tharpep/sazed-frontend"])])
    assert (out.rule, out.link, out.notes()) == (None, [], [])


def test_main_and_master_never_match():
    assert autolink.matchable_branch("main") is None
    assert autolink.matchable_branch("master") is None


def test_a_card_line_wins_over_the_id_in_the_branch():
    out = _choose([str(A)], [_card(A)], "7d0e44aa", [_card(B)])
    assert (out.rule, out.link) == ("card_line", [A])


def test_two_card_lines_link_both_cards():
    out = _choose([str(A), "7d0e44aa"], [_card(A), _card(B)])
    assert out.link == [A, B]


def test_a_card_line_naming_a_missing_card_falls_through_to_the_branch():
    out = _choose(["deadbeef"], [], "3fa9c1b2", [_card(A)])
    assert (out.rule, out.link) == ("branch_id", [A])


def test_two_open_cards_on_the_branch_are_ambiguous():
    out = _choose(by_branch=[_card(B), _card(A)])
    assert out.link == [] and out.ambiguous == [[A, B]]
    assert out.notes() == [f"link ambiguous by branch: item:{A}, item:{B}"]


def test_a_card_already_on_this_pr_is_a_no_op():
    out = _choose(by_branch=[_card(A, pr_repo="tharpep/sazed", pr_number=12), _card(B)])
    assert (out.link, out.already, out.notes()) == ([], [A], [])


def test_a_closed_card_on_this_pr_does_not_block_its_replacement():
    dismissed = _card(A, status="dismissed", pr_repo="tharpep/sazed", pr_number=12)
    out = _choose(by_branch=[dismissed, _card(B)])
    assert (out.link, out.already) == ([B], [])


def test_a_done_card_on_this_pr_named_again_is_silent():
    out = _choose([str(A)], [_card(status="done", pr_repo="tharpep/sazed", pr_number=12)])
    assert (out.link, out.notes()) == ([], [])


def test_a_card_on_another_pr_is_skipped_and_named():
    out = _choose(["3fa9c1b2"], [_card(pr_repo="tharpep/sazed", pr_number=9)])
    assert out.link == []
    assert out.notes() == [f"item:{A} already on tharpep/sazed#9"]


def test_a_closed_card_named_by_a_card_line_says_why():
    out = _choose([str(A)], [_card(status="done")])
    assert out.notes() == [f"item:{A} is done"]


def test_a_closed_card_on_the_same_branch_is_silent():
    assert _choose(by_branch=[_card(status="done")]).notes() == []


def test_the_slug_is_cut_on_a_word_edge():
    title = "Link pull requests to their board cards automatically on open"
    assert autolink.slug(title) == "link-pull-requests-to-their-board-cards"
    assert autolink.slug("x" * 60) == "x" * 40
    assert autolink.slug("Café déjà vu!") == "cafe-deja-vu"
    assert autolink.suggested_branch(A, "日本語") == "feature/3fa9c1b2"


def _title(rng: random.Random) -> str:
    alphabet = "abcXYZ019 -_/.:!é日本語ßΩ🙂\t"
    return "".join(rng.choice(alphabet) for _ in range(rng.choice([1, 5, 40, 120, 200])))


def test_every_suggested_branch_reads_back_to_its_card():
    rng = random.Random(20261125)
    titles = [_title(rng) for _ in range(98)] + ["é" * 200, "x" * 200]
    for title in titles:
        card = _card(uuid.UUID(int=rng.getrandbits(128)))
        branch = autolink.suggested_branch(card["id"], title)
        assert re.fullmatch(r"feature/[0-9a-f]{8}(-[a-z0-9-]{1,40})?", branch), branch
        short = autolink.branch_id(branch)
        out = _choose(short=short, by_short=[card], by_branch=[])
        assert out.link == [card["id"]], (title, branch)


def test_the_server_actor_is_barred_from_the_api():
    assert not re.match(events.ACTOR, events.SERVER_ACTOR)
    assert {"pr_linked", "pr_link_ambiguous"} <= set(events.KINDS)


async def test_post_backlog_returns_the_suggested_branch():
    item = {"id": A, "title": "Link PRs to cards", "status": "new"}
    body = router.ItemCreate(type="planned", title="Link PRs to cards")
    with (
        patch.object(router.service, "add_item", AsyncMock(return_value=(item, None))),
        patch.object(router.store, "find_duplicate", AsyncMock(return_value=None)),
    ):
        out = await router.create_item(body)
    assert out["suggested_branch"] == "feature/3fa9c1b2-link-prs-to-cards"
    assert autolink.branch_id(out["suggested_branch"]) == str(A)[:8]
