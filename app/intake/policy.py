"""Who may start work and what an event starts (plans/16): the sender gate runs before any text."""

import re
from dataclasses import dataclass
from typing import Literal

ALLOWED = {"github": frozenset({"tharpep"})}
RECORDED_BOTS = {"github": frozenset({"dependabot[bot]"})}
OWN_BOTS = {"github": frozenset({"sazed-worker-tharpep[bot]"})}

REVIEWED_PR_ACTIONS = ("opened", "synchronize", "ready_for_review")
_MENTION = re.compile(r"(?<![\w@])@sazed\b", re.IGNORECASE)

Sender = Literal["person", "recorded_bot", "own_bot", "unknown"]
Verdict = Literal["accept", "observe", "ignore"]
Action = Literal["review", "follow_up", "new_task", "backlog_card"]


@dataclass(frozen=True)
class GithubEvent:
    event: str
    action: str
    repo: str
    number: int
    is_pr: bool
    sender: str
    title: str = ""
    body: str = ""
    branch: str | None = None
    draft: bool = False
    url: str = ""
    path: str | None = None
    line: int | None = None

    @property
    def thread_key(self) -> str:
        return f"github:{self.repo}#{self.number}"


@dataclass(frozen=True)
class Facts:
    """The work this thread already belongs to, looked up before deciding."""

    owner_job_id: str | None = None
    owner_continuable: bool = False
    review_open: bool = False


@dataclass(frozen=True)
class Decision:
    verdict: Verdict
    reason: str
    action: Action | None = None


def sender_kind(source: str, login: str) -> Sender:
    if login in OWN_BOTS.get(source, ()):
        return "own_bot"
    if login in ALLOWED.get(source, ()):
        return "person"
    if login in RECORDED_BOTS.get(source, ()):
        return "recorded_bot"
    return "unknown"


def mentions_sazed(text: str) -> bool:
    return bool(_MENTION.search(text or ""))


def _is_comment(ev: GithubEvent) -> bool:
    return (
        (ev.event == "issue_comment" and ev.action == "created")
        or (ev.event == "pull_request_review_comment" and ev.action == "created")
        or (ev.event == "pull_request_review" and ev.action == "submitted")
    )


def decide(ev: GithubEvent, facts: Facts) -> Decision:
    kind = sender_kind("github", ev.sender)
    if kind == "own_bot":
        return Decision("ignore", "Sazed's own bot")
    if kind == "unknown":
        return Decision("ignore", f"{ev.sender or 'no sender'} isn't on the allowed list")
    if kind == "recorded_bot":
        return Decision("observe", "bot events are recorded, never acted on")

    if ev.event == "pull_request":
        if ev.action not in REVIEWED_PR_ACTIONS:
            return Decision("ignore", f"action {ev.action}")
        if ev.draft:
            return Decision("ignore", "draft")
        if facts.owner_job_id:
            return Decision("ignore", "Sazed's own PR; its CI and comments drive it")
        if facts.review_open:
            return Decision("ignore", "a review of this PR is already queued")
        return Decision("accept", "PR from Pryce", "review")

    if _is_comment(ev):
        if not ev.body.strip():
            return Decision("ignore", "no text")
        if facts.owner_job_id:
            if facts.owner_continuable:
                return Decision("accept", "comment on work Sazed owns", "follow_up")
            return Decision(
                "observe", "the owning job hasn't stopped yet; comment again once it has"
            )
        if mentions_sazed(ev.body):
            return Decision("accept", "@sazed mention", "new_task")
        return Decision("ignore", "no owning work and no @sazed mention")

    if ev.event == "issues" and ev.action == "opened":
        if mentions_sazed(ev.body) or mentions_sazed(ev.title):
            return Decision("accept", "@sazed mention", "new_task")
        return Decision("accept", "issue from Pryce", "backlog_card")

    return Decision("ignore", f"{ev.event} {ev.action} isn't routed")
