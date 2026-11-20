"""A card's PR: which PR it points at, the snapshot the CI watcher keeps of it, the review
verdict, and the `pr` block cards carry. Pure functions; the SQL is in `store`, the fetching in
`jobs.ci_watch`. Gateway replies, review text and card text are untrusted: nothing here raises
on a malformed value."""

import json
import re
from datetime import datetime, timedelta
from typing import Any

Key = tuple[str, int]

# ci_watch checks a PR again once its snapshot is this old; cards show the age past it.
SNAPSHOT_AGE = timedelta(minutes=6)

_CHECK_STATES = {"success": "passing", "failure": "failing", "pending": "running"}
# The gateway's own rule for a run that doesn't fail the commit (routers/github.py ci_status).
_PASSED = ("success", "skipped", "neutral")


def key(repo: str, number: int) -> Key:
    return repo.lower(), int(number)


def _job_ref(chain: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The newest PR in the chain; a follow-up doesn't always repeat it."""
    for job in reversed(chain):
        meta = job.get("meta") or {}
        repo = (job.get("params") or {}).get("repo") or (chain[0].get("params") or {}).get("repo")
        if meta.get("pr_number") and repo:
            if meta.get("ci_state") in ("merged", "closed"):
                known = meta["ci_state"]
            else:
                known = "draft" if meta.get("pr_draft") else "open"
            return {"repo": repo, "number": int(meta["pr_number"]), "known": known}
    return None


def item_ref(item: dict[str, Any], chain: list[dict[str, Any]]) -> dict[str, Any] | None:
    """A done item keeps its job's PR; an item reopened after a closed PR drops it, as its
    stage does (stages.item_stage), and falls back to the PR a session linked."""
    if item["status"] in ("approved", "done") and (found := _job_ref(chain)):
        return found
    if item.get("pr_number") and item.get("pr_repo"):
        known = "merged" if item["status"] == "done" else "open"
        return {"repo": item["pr_repo"], "number": int(item["pr_number"]), "known": known}
    return None


def job_ref(job: dict[str, Any]) -> dict[str, Any] | None:
    return _job_ref([job])


def block(ref: dict[str, Any] | None, snap: dict[str, Any] | None) -> dict[str, Any] | None:
    """The card's `pr` field. Before the first fetch, state is what Sazed already knows."""
    if ref is None:
        return None
    url = f"https://github.com/{ref['repo']}/pull/{ref['number']}"
    out = {"repo": ref["repo"], "number": ref["number"], "url": url}
    if snap is None:
        return out | {
            "state": ref["known"],
            "checks": None,
            "review": None,
            "additions": None,
            "deletions": None,
            "merged_at": None,
            "fetched_at": None,
        }
    review = None
    if snap["review_verdict"]:
        review = {
            "verdict": snap["review_verdict"],
            "source": snap["review_source"],
            "url": snap["review_url"] or url,
        }
    return out | {
        "state": snap["state"],
        "checks": snap["checks"],
        "review": review,
        "additions": snap["additions"],
        "deletions": snap["deletions"],
        "merged_at": snap["merged_at"],
        "fetched_at": snap["fetched_at"],
    }


def _wanted(snap: dict[str, Any] | None, done_at: datetime | None, now: datetime) -> bool:
    if snap is None:
        return True
    if snap["state"] in ("merged", "closed"):
        return False
    if done_at is not None:
        return snap["fetched_at"] < done_at
    return now - snap["fetched_at"] >= SNAPSHOT_AGE


def due(
    items: list[dict[str, Any]],
    chains: dict[Any, list[dict[str, Any]]],
    jobs: list[dict[str, Any]],
    snaps: dict[Key, dict[str, Any]],
    now: datetime,
    done_window: timedelta,
) -> list[tuple[dict[str, Any], dict[str, Any] | None]]:
    """PRs to fetch, never-fetched first, then the oldest snapshot. An open card's PR is fetched
    every SNAPSHOT_AGE; a done card's once after it turned done, and only while the board still
    shows it; a merged or closed snapshot never again."""
    found: dict[Key, dict[str, Any]] = {}
    for item in items:
        ref = item_ref(item, chains.get(item["id"], []))
        if ref is None:
            continue
        done_at = None
        if item["status"] == "done":
            done_at = item["decided_at"] or item["updated_at"]
            if now - done_at > done_window:
                continue
        if _wanted(snaps.get(key(ref["repo"], ref["number"])), done_at, now):
            found.setdefault(key(ref["repo"], ref["number"]), ref)
    for job in jobs:
        ref = job_ref(job)
        if ref and _wanted(snaps.get(key(ref["repo"], ref["number"])), None, now):
            found.setdefault(key(ref["repo"], ref["number"]), ref)
    pairs = [(ref, snaps.get(k)) for k, ref in found.items()]
    return sorted(pairs, key=lambda p: (p[1] is not None, p[1]["fetched_at"] if p[1] else now))


def checks(ci: Any) -> dict[str, Any]:
    """The gateway's /ci reply as the card's checks; no reply (no head sha) reads as none."""
    ci = ci if isinstance(ci, dict) else {}
    runs = [r for r in ci.get("runs") or [] if isinstance(r, dict)]
    return {
        "state": _CHECK_STATES.get(ci.get("state"), "none"),
        "passed": sum(r.get("conclusion") in _PASSED for r in runs),
        "total": len(runs),
        "runs": [
            {"name": str(r.get("name") or ""), "conclusion": r.get("conclusion")} for r in runs
        ],
    }


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _time(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


def head_sha(pr: dict[str, Any]) -> str | None:
    sha = pr.get("head_sha")
    return sha if isinstance(sha, str) and sha else None


def snapshot(
    pr: dict[str, Any], pr_checks: dict[str, Any], review: dict[str, Any] | None
) -> dict[str, Any]:
    """The row to store from the gateway's PR reply. A gateway that predates head_sha and the
    diff counts still gives state and merged time."""
    if pr.get("merged"):
        state = "merged"
    elif pr.get("state") == "closed":
        state = "closed"
    else:
        state = "draft" if pr.get("draft") else "open"
    return {
        "state": state,
        "head_sha": head_sha(pr),
        "merged_at": _time(pr.get("merged_at")),
        "additions": _int(pr.get("additions")),
        "deletions": _int(pr.get("deletions")),
        "checks": pr_checks,
        "review_verdict": (review or {}).get("verdict"),
        "review_source": (review or {}).get("source"),
        "review_url": (review or {}).get("url"),
    }


_JSON_BLOCK = re.compile(r"```json\s*(.*?)```", re.DOTALL)
_VERDICT_LINE = re.compile(r"^#+\s*Verdict\s*$\s*\**([A-Za-z ]+)", re.MULTILINE)


def _verdict_word(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    words = " ".join(re.findall(r"[a-z]+", value.lower()))
    if words in ("approve", "approved"):
        return "approve"
    if words in ("approve with nits", "approved with nits"):
        return "approve_with_nits"
    if words in ("request changes", "changes requested"):
        return "request_changes"
    return None


def parse_verdict(text: Any) -> str | None:
    """The verdict of a review job's result (jobs.service._PR_REVIEW_PROMPT): the JSON block's
    `verdict` first, then the bold word under `## Verdict`. None when neither reads cleanly."""
    if not isinstance(text, str):
        return None
    for raw in reversed(_JSON_BLOCK.findall(text)):
        try:
            data = json.loads(raw)
        except (ValueError, RecursionError):
            continue
        if isinstance(data, dict) and (word := _verdict_word(data.get("verdict"))):
            return word
    if found := _VERDICT_LINE.search(text):
        return _verdict_word(found.group(1))
    return None


def review(job: dict[str, Any] | None) -> dict[str, Any] | None:
    """The stored review job as the card's review; no job or no readable verdict is no review."""
    if job is None or not (verdict := parse_verdict(job.get("result"))):
        return None
    meta = job.get("meta") or {}
    return {"verdict": verdict, "source": job.get("agent"), "url": meta.get("pr_review_url")}
