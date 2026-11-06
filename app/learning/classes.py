"""The closed failure vocabulary, and the code rules that put a finished job into one class."""

import re
from dataclasses import dataclass
from typing import Any, Literal


@dataclass(frozen=True)
class FailureClass:
    family: Literal["stop", "lesson"]
    label: str
    guard_hint: str = ""


CLASSES: dict[str, FailureClass] = {
    "timed_out": FailureClass("stop", "Hit the job timeout"),
    "tool_cap": FailureClass("stop", "Hit the tool-call cap"),
    "rate_limit": FailureClass("stop", "Hit the plan rate limit"),
    "quota_exhausted": FailureClass("stop", "Usage quota used up"),
    "shutdown": FailureClass("stop", "Worker shut down mid-job"),
    "cancelled": FailureClass("stop", "Cancelled"),
    "worker_error": FailureClass(
        "lesson",
        "Worker error",
        "a worker preflight check that catches the failing setup step before the agent starts",
    ),
    "worker_lost": FailureClass(
        "lesson",
        "Worker stopped reporting",
        "a heartbeat or progress check that surfaces a silent worker before the job expires",
    ),
    "agent_error": FailureClass(
        "lesson",
        "Agent error",
        "a test or harness check on the agent's failing step, so the error is caught early",
    ),
    "no_change": FailureClass(
        "lesson",
        "Change job changed nothing",
        "a harness check that rejects a finished change job with no changed files",
    ),
    "checks_failed": FailureClass(
        "lesson",
        "Repo checks failed",
        "a repo rule or check-specific instruction for the finding that keeps surviving the "
        "worker's fix round",
    ),
    "ci_failed": FailureClass(
        "lesson",
        "CI failed after the fix attempts",
        "a local run of the CI workflow's commands before the PR opens",
    ),
    "pr_gone": FailureClass(
        "lesson",
        "PR closed or branch gone",
        "a check that the PR is still open before the job commits to its branch",
    ),
    "pr_open_failed": FailureClass(
        "lesson",
        "PR could not be opened",
        "a dry-run of the PR-open call (branch, base, permissions) before the work starts",
    ),
    "review_post_failed": FailureClass(
        "lesson",
        "Review could not be posted",
        "a check that the review body fits the GitHub API limits before posting",
    ),
    "result_unusable": FailureClass(
        "lesson",
        "Reply did not fit the result format",
        "a schema check on the final reply that triggers the repair round",
    ),
    "unclassified": FailureClass("lesson", "Unclassified failure"),
}

_QUOTA = re.compile(r"quota|usage limit", re.I)
_PR_GONE = re.compile(r"closed|merged|not reopening|Reference does not exist", re.I)
_META_STOPS = ("timed_out", "tool_cap", "rate_limit", "shutdown")


def classify(job: dict[str, Any]) -> str | None:
    """The first matching rule's class, or None for a job that didn't go wrong."""
    status, profile = job.get("status"), job.get("profile")
    error = job.get("error") or ""
    meta = job.get("meta") or {}
    params = job.get("params") or {}

    if status == "cancelled":
        return "cancelled"
    for key in ("stop_reason", "pause_reason"):
        if meta.get(key) in _META_STOPS:
            return meta[key]
    if status == "timed_out":
        if error == "hit the job timeout":
            return "timed_out"
        if error == "worker stopped reporting":
            return "worker_lost"
    if status == "failed":
        if _QUOTA.search(error):
            return "quota_exhausted"
        return "worker_error" if error.startswith("worker error:") else "agent_error"
    if meta.get("pr_review_error"):
        return "review_post_failed"
    if pr_error := meta.get("pr_error"):
        return "pr_gone" if _PR_GONE.search(str(pr_error)) else "pr_open_failed"
    if (meta.get("check_result") or {}).get("passed") is False:
        return "checks_failed"
    if meta.get("ci_state") == "failure":
        return "ci_failed"
    if meta.get("result_format") == "prose":
        return "result_unusable"
    if (
        profile == "change"
        and status == "succeeded"
        and not params.get("result_only")
        and meta.get("changed_files") == 0
    ):
        return "no_change"
    if status in ("failed", "timed_out"):
        return "unclassified"
    return None
