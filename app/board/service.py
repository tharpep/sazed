"""The board (plans/13): one flat card shape from every source, read by the console and the MCP.

A new kind of work is one more source here. Stages come from `stages`, never from the clients.
Cards carry `actions` as data; phase 1 is read-only, so the list is empty.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from app.backlog import store as backlog
from app.board import stages, store
from app.jobs import store as jobs

DONE_WINDOW = timedelta(days=14)

_NEEDS_ME_FIRST = {"needs_input": 0, "needs_approval": 1}


def _short_repo(repo: str | None) -> str | None:
    return repo.split("/")[-1] if repo else None


def _pr_fields(chain: list[dict[str, Any]]) -> dict[str, Any]:
    """The newest PR details in the chain; a follow-up doesn't always repeat them."""
    for job in reversed(chain):
        meta = job.get("meta") or {}
        if meta.get("pr_number"):
            return {
                "pr_number": meta["pr_number"],
                "pr_url": meta.get("pr_url"),
                "pr_draft": bool(meta.get("pr_draft")),
            }
    return {"pr_number": None, "pr_url": None, "pr_draft": False}


def _job_fields(chain: list[dict[str, Any]]) -> dict[str, Any]:
    if not chain:
        return {
            "job_id": None,
            "job_status": None,
            "ci_state": None,
            "fix_count": 0,
            **_pr_fields([]),
        }
    tail = chain[-1]
    return {
        "job_id": str(tail["id"]),
        "job_status": tail["status"],
        "ci_state": (tail.get("meta") or {}).get("ci_state"),
        "fix_count": sum(j["requested_by"].startswith("ci-fix:") for j in chain),
        **_pr_fields(chain),
    }


def _item_card(item: dict[str, Any], chain: list[dict[str, Any]]) -> dict[str, Any]:
    live = chain if item["status"] == "approved" else []
    tail = live[-1] if live else None
    stage, next_step = stages.item_stage(item, tail)
    times = [item["updated_at"]]
    times += [t for job in live for t in (job["created_at"], job["finished_at"]) if t]
    repo = (tail.get("params") or {}).get("repo") if tail else None
    return {
        "id": f"item:{item['id']}",
        "kind": "item",
        "source": "backlog",
        "title": item["title"],
        "stage": stage,
        "next": next_step,
        "updated_at": max(times),
        "repo": _short_repo(repo or (item["repos"][0] if item["repos"] else None)),
        "item_type": item["type"],
        "effort": item["effort"],
        "impact": item["impact"],
        "rank": item["rank"],
        "actions": [],
        **_job_fields(live),
    }


def _job_card(job: dict[str, Any]) -> dict[str, Any]:
    stage, next_step = stages.job_stage(job)
    return {
        "id": f"job:{job['id']}",
        "kind": "job",
        "source": "jobs",
        "title": job["title"],
        "stage": stage,
        "next": next_step,
        "updated_at": job["finished_at"] or job["started_at"] or job["created_at"],
        "repo": _short_repo((job.get("params") or {}).get("repo")),
        "item_type": None,
        "effort": None,
        "impact": None,
        "rank": None,
        "actions": [],
        **_job_fields([job]),
    }


def _shows_alone(job: dict[str, Any], linked: set[Any]) -> bool:
    """Automations run all day, so their jobs only show when they're waiting on Pryce."""
    if job["id"] in linked:
        return False
    if job["requested_by"].startswith("automation"):
        return stages.job_stage(job)[0] == "needs_me"
    return True


def _order(card: dict[str, Any]) -> tuple:
    return (
        card["rank"] is None,
        card["rank"] or 0,
        _NEEDS_ME_FIRST.get(card["job_status"], 2),
        -card["updated_at"].timestamp(),
    )


async def cards(*, all_done: bool = False) -> list[dict[str, Any]]:
    items = await store.items()
    chains = await store.chains()
    active = await store.active_jobs()
    linked = {job["id"] for chain in chains.values() for job in chain}
    result = [_item_card(item, chains.get(item["id"], [])) for item in items]
    result += [_job_card(job) for job in active if _shows_alone(job, linked)]
    if not all_done:
        cutoff = datetime.now(UTC) - DONE_WINDOW
        result = [c for c in result if c["stage"] != "done" or c["updated_at"] >= cutoff]
    return sorted(result, key=_order)


async def card(card_id: str) -> dict[str, Any] | None:
    """One card with the detail a reader needs: the item's text, or the job's question."""
    found = next((c for c in await cards(all_done=True) if c["id"] == card_id), None)
    if found is None:
        return None
    kind, _, raw_id = card_id.partition(":")
    if kind == "item":
        item = await backlog.get_item(uuid.UUID(raw_id))
        found |= {"summary": item["summary"], "why": item["why"], "note": item["note"]}
    if found["job_id"]:
        job = await jobs.get_job(uuid.UUID(found["job_id"]))
        meta = job.get("meta") or {}
        found |= {
            "question": meta.get("question") if job["status"] == "needs_input" else None,
            "tests": meta.get("tests"),
            "checks": meta.get("checks"),
        }
    return found


def board(all_cards: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "stages": list(stages.STAGES),
        "counts": {s: sum(c["stage"] == s for c in all_cards) for s in stages.STAGES},
        "cards": all_cards,
    }
