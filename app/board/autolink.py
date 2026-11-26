"""Link a pull request to its board card with no model: the matching rules, pure, no I/O.

A PR names its card by a `Card:` line in its body, by a card id at the start of its branch's last
segment, or by sharing the card's linked branch. The first rule that finds a card decides. Titles,
bodies and branches are untrusted: they are only matched against fixed patterns here.
"""

import re
import unicodedata
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

Rule = Literal["card_line", "branch_id", "branch"]
RULES: tuple[Rule, ...] = ("card_line", "branch_id", "branch")

OPEN = ("new", "shortlisted")

# The gateway already cuts the body to 8000 characters; this holds if it ever stops.
BODY_LIMIT = 8000
# GitHub links at most 10 issues per PR; one PR naming more cards than that is a mistake.
MAX_CARD_LINES = 10
BRANCH_LIMIT = 255
SLUG_LIMIT = 40

UNMATCHED_BRANCHES = frozenset({"main", "master"})

_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_CARD_LINE = re.compile(
    rf"^[ \t]*card[ \t]*:[ \t]*(?:item:)?(?P<ref>{_UUID}|[0-9a-f]{{8}})[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)
_BRANCH_ID = re.compile(r"^(?P<id>[0-9a-f]{8})(?:-|$)", re.IGNORECASE)
_NOT_SLUG = re.compile(r"[^a-z0-9]+")


def card_refs(body: str) -> list[str]:
    """The card ids a PR body names, lowercase, deduped, in order, at most MAX_CARD_LINES.
    Each is a full uuid or an 8-hex short id."""
    refs: list[str] = []
    for m in _CARD_LINE.finditer((body or "")[:BODY_LIMIT]):
        ref = m["ref"].lower()
        if ref not in refs:
            refs.append(ref)
        if len(refs) == MAX_CARD_LINES:
            break
    return refs


def matchable_branch(branch: str | None) -> str | None:
    if not branch or len(branch) > BRANCH_LIMIT or branch in UNMATCHED_BRANCHES:
        return None
    return branch


def branch_id(branch: str | None) -> str | None:
    """The 8-hex card id that starts the branch's last path segment, lowercase."""
    branch = matchable_branch(branch)
    if branch is None:
        return None
    m = _BRANCH_ID.match(branch.rsplit("/", 1)[-1])
    return m["id"].lower() if m else None


def slug(title: str) -> str:
    ascii_title = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    s = _NOT_SLUG.sub("-", ascii_title.lower()).strip("-")
    if len(s) <= SLUG_LIMIT:
        return s
    cut = s[:SLUG_LIMIT]
    if s[SLUG_LIMIT] != "-" and "-" in cut:
        cut = cut.rsplit("-", 1)[0]
    return cut.strip("-")


def suggested_branch(item_id: uuid.UUID | str, title: str) -> str:
    short = str(item_id).lower()[:8]
    words = slug(title)
    return f"feature/{short}-{words}" if words else f"feature/{short}"


def repo_name(repo: str | None) -> str:
    return (repo or "").rsplit("/", 1)[-1].lower()


def _in_repo(card: dict[str, Any], repo: str) -> bool:
    repos = card.get("repos") or []
    return not repos or repo_name(repo) in {repo_name(r) for r in repos}


def _on_pr(card: dict[str, Any], repo: str, number: int) -> bool:
    return card.get("pr_number") == number and repo_name(card.get("pr_repo")) == repo_name(repo)


@dataclass
class Outcome:
    rule: Rule | None = None
    link: list[uuid.UUID] = field(default_factory=list)
    already: list[uuid.UUID] = field(default_factory=list)
    ambiguous: list[list[uuid.UUID]] = field(default_factory=list)
    skipped: list[tuple[uuid.UUID, str]] = field(default_factory=list)

    def notes(self) -> list[str]:
        """The trigger reason's parts; empty when the PR names no card."""
        out = [f"linked item:{i} by {self.rule}" for i in self.link]
        out += [
            f"link ambiguous by {self.rule}: " + ", ".join(f"item:{i}" for i in group)
            for group in self.ambiguous
        ]
        out += [f"item:{i} {why}" for i, why in self.skipped]
        return out


def _resolve(
    found: list[dict[str, Any]], repo: str, number: int, explicit: bool, out: Outcome
) -> None:
    """One name's matching cards into the outcome. A card already on this PR settles it; two or
    more open unlinked cards are ambiguous. Closed cards are reported only for explicit names."""
    if any(_on_pr(c, repo, number) for c in found):
        out.already += [c["id"] for c in found if _on_pr(c, repo, number)]
        return
    open_cards = [c for c in found if c["status"] in OPEN]
    free = [c for c in open_cards if c.get("pr_number") is None]
    if len(free) >= 2:
        out.ambiguous.append(sorted((c["id"] for c in free), key=str))
        return
    if free:
        out.link.append(free[0]["id"])
        return
    for c in open_cards:
        out.skipped.append((c["id"], f"already on {c['pr_repo']}#{c['pr_number']}"))
    if explicit and len(found) == 1 and not open_cards:
        out.skipped.append((found[0]["id"], f"is {found[0]['status']}"))


def _named_by(card_id: uuid.UUID, ref: str) -> bool:
    return str(card_id).startswith(ref)


def choose(
    repo: str,
    number: int,
    refs: list[str],
    by_ref: list[dict[str, Any]],
    short_id: str | None,
    by_short_id: list[dict[str, Any]],
    by_branch: list[dict[str, Any]],
) -> Outcome:
    """Pick the cards to link. Each list holds the cards one rule's lookup returned, any status;
    the first rule whose list holds a card decides and the lower rules are not consulted."""
    named = [(ref, [c for c in by_ref if _named_by(c["id"], ref)]) for ref in refs]
    if any(found for _, found in named):
        out = Outcome(rule="card_line")
        for _, found in named:
            if found:
                _resolve(found, repo, number, explicit=True, out=out)
        out.link = list(dict.fromkeys(out.link))
        return out
    if short_id:
        found = [c for c in by_short_id if _named_by(c["id"], short_id)]
        if found:
            out = Outcome(rule="branch_id")
            _resolve(found, repo, number, explicit=True, out=out)
            return out
    in_repo = [c for c in by_branch if _in_repo(c, repo)]
    out = Outcome(rule="branch" if in_repo else None)
    if in_repo:
        _resolve(in_repo, repo, number, explicit=False, out=out)
    return out
