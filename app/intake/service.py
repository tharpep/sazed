"""Route one inbound event: look up its thread's work, decide in code, act, and log the outcome."""

import uuid
from typing import Any

from app.backlog import service as backlog
from app.backlog import store as backlog_store
from app.intake import policy, store
from app.jobs import service as jobs
from app.jobs import store as job_store

_CONTINUABLE = ("succeeded", "needs_input", "cancelled", "timed_out", "paused")


async def _facts(ev: policy.GithubEvent) -> policy.Facts:
    owner = await store.owner_of_thread(ev.thread_key)
    if owner is None and ev.is_pr:
        owner = await store.owner_of_pr(ev.repo, ev.number)
    review_open = ev.event == "pull_request" and await job_store.pr_review_open(ev.repo, ev.number)
    return policy.Facts(
        owner_job_id=str(owner["id"]) if owner else None,
        owner_continuable=bool(
            owner and owner["status"] in _CONTINUABLE and owner.get("agent_session_id")
        ),
        review_open=review_open,
    )


def _where(ev: policy.GithubEvent) -> str:
    kind = "PR" if ev.is_pr else "issue"
    spot = f" ({ev.path}:{ev.line})" if ev.path else ""
    return f"{kind} #{ev.number}{spot} in {ev.repo}"


async def _act(ev: policy.GithubEvent, action: str, facts: policy.Facts) -> dict[str, Any]:
    requested_by = f"webhook:{ev.thread_key}"
    if action == "review":
        instruction = f"Review PR #{ev.number}: {ev.title}\n\n{ev.body}".strip()
        job = await jobs.create_code_task(
            ev.repo, instruction, "review", "webhook", pr_number=ev.number, branch=ev.branch
        )
        return {"job_id": job["id"]}
    if action == "follow_up":
        instruction = f"Pryce commented on {_where(ev)}:\n\n{ev.body.strip()}"
        job = await jobs.create_follow_up(
            uuid.UUID(facts.owner_job_id), instruction, requested_by, approved=True
        )
        return {"job_id": job["id"]}
    if action == "new_task":
        instruction = (
            f"{ev.title}\n\n{ev.body}".strip() if ev.event == "issues" else ev.body.strip()
        )
        context = f"Asked for on {_where(ev)}: {ev.url}" if ev.url else f"Asked for on {_where(ev)}"
        job = await jobs.create_code_task(
            ev.repo, instruction, "change", requested_by, context=context
        )
        await store.remember_thread(ev.thread_key, job_id=job["id"])
        return {"job_id": job["id"]}
    item, duplicate = await backlog.add_item(
        type="planned",
        title=ev.title or f"Issue #{ev.number} in {ev.repo}",
        source="github",
        summary=ev.body,
        repos=[ev.repo],
        sources=[ev.url] if ev.url else [],
    )
    found = item or duplicate
    await store.remember_thread(ev.thread_key, item_id=found["id"])
    return {"item_id": found["id"], "duplicate": duplicate is not None}


async def route_github(ev: policy.GithubEvent) -> dict[str, Any]:
    closed = []
    if ev.event == "pull_request" and ev.action == "closed" and ev.merged:
        closed = await backlog_store.close_for_merged_pr(ev.repo, ev.number)
    facts = await _facts(ev)
    decision = policy.decide(ev, facts)
    refs: dict[str, Any] = {}
    reason = decision.reason
    verdict = decision.verdict
    if verdict == "accept" and decision.action:
        try:
            refs = await _act(ev, decision.action, facts)
        except jobs.JobError as e:
            verdict, reason = "ignore", f"{decision.action} refused: {e}"
        if refs.get("duplicate"):
            reason = "already in the backlog; linked the existing card"
    row = await store.record(
        source="github",
        event=ev.event,
        action=ev.action,
        actor=ev.sender,
        thread_key=ev.thread_key,
        decision=verdict,
        reason=reason,
        action_taken=decision.action if verdict == "accept" else None,
        job_id=refs.get("job_id"),
        item_id=refs.get("item_id") or (closed[0] if closed else None),
    )
    return {
        "decision": verdict,
        "reason": reason,
        "action": row["action_taken"],
        "job_id": str(row["job_id"]) if row["job_id"] else None,
        "item_id": str(row["item_id"]) if row["item_id"] else None,
        "trigger_id": str(row["id"]),
    }
