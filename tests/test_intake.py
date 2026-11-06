"""The intake's gate and routing rules (plans/16): sender first, then the thread's work."""

import pytest

from app.intake import policy
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
        (Facts(), {"draft": True}, "ignore"),
        (Facts(), {"action": "closed"}, "ignore"),
        (Facts(review_open=True), {}, "ignore"),
        (OWNED, {}, "ignore"),
    ],
)
def test_pull_requests_from_pryce_get_one_review(facts, kw, verdict):
    ev = _ev(**({"event": "pull_request", "action": "opened"} | kw))
    assert policy.decide(ev, facts).verdict == verdict


def test_thread_keys_name_the_repo_and_number():
    assert _ev().thread_key == "github:tharpep/sazed#7"
