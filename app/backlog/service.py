"""Backlog rules: dedupe on add, and approval turning an item into a change job."""

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx

from app.backlog import store
from app.board import events
from app.config import settings
from app.db import get_pool
from app.http_client import get_client
from app.jobs import service as jobs

logger = logging.getLogger(__name__)


class BacklogError(ValueError):
    """An action the item's state doesn't allow (approving a dismissed item, ...)."""


async def add_item(
    *,
    type: str,
    title: str,
    source: str,
    duplicate_statuses: tuple[str, ...] | None = None,
    allow_similar: bool = False,
    **fields: Any,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Returns (new item, None), or (None, the existing item) when it's already recorded.

    `duplicate_statuses` limits which items count (default all, which automated filers rely on
    so dismissed ideas stay dismissed); `allow_similar` skips the check.
    """
    if not allow_similar:
        duplicate = await store.find_duplicate(title, duplicate_statuses)
        if duplicate:
            return None, duplicate
    return await store.create_item(type_=type, title=title, source=source, **fields), None


def _instruction(item: dict[str, Any], note: str) -> str:
    # The first line becomes the PR title, so it's the item's title alone.
    parts = [item["title"], "This is an item from Pryce's backlog that he approved."]
    if item.get("summary"):
        parts.append(f"What it is:\n{item['summary']}")
    if item.get("why"):
        parts.append(f"Why:\n{item['why']}")
    if item.get("sources"):
        parts.append("References:\n" + "\n".join(f"- {u}" for u in item["sources"]))
    if note.strip():
        parts.append(f"Pryce's note (takes priority over the above):\n{note.strip()}")
    parts.append(
        "If it turns out to need changes in another repo too, make the part that belongs in "
        "this one and say in the PR description what else is needed."
    )
    return "\n\n".join(parts)


async def approve(
    item_id: uuid.UUID, repo: str, note: str = "", model: str | None = None
) -> dict[str, Any]:
    item = await store.get_item(item_id)
    if item is None:
        raise BacklogError("item not found")
    if item["status"] not in store.OPEN:
        raise BacklogError(f"item is {item['status']}; restore it first")
    # Pryce approved it in the console, so the job starts now (uncapped, no second approval).
    job = await jobs.create_code_task(
        repo, _instruction(item, note), "change", "console", model=model
    )
    await _record_worker_job_start(item_id, str(job["id"]))
    return await store.update_item(
        item_id, status="approved", job_id=job["id"], note=note.strip() or None
    )


# Plan states (migration 041, slice 4). Approving a plan says the design is right and starts
# nothing; starting hands the card to an orchestrator and creates no job or dispatch run.

_TAKES_PLAN_STATES = (*store.OPEN, "approved")


class NotFound(BacklogError):
    """No item with that id."""


class Forbidden(BacklogError):
    """The actor may not do this (an agent withdrawing)."""


async def _locked_state(conn, item_id: uuid.UUID) -> dict[str, Any]:
    status = await store.lock_status(conn, item_id)
    if status is None:
        raise NotFound("item not found")
    if status not in _TAKES_PLAN_STATES:
        raise BacklogError(f"item is {status}; restore it first")
    return await events.state(item_id, conn)


async def _approve(conn, item_id: uuid.UUID, st: dict[str, Any], by: str, **detail: Any) -> None:
    version = st["plan_current_version"] if st["plan_slug"] else None
    await store.set_plan_state(
        conn,
        item_id,
        plan_approved_at=datetime.now(UTC),
        plan_approved_by=by,
        plan_approved_version=version,
    )
    await events.record(
        conn,
        item_id,
        "plan_approved",
        by,
        {"plan": st["plan_slug"], "section": st["plan_section"], "version": version, **detail},
    )


async def _start(conn, item_id: uuid.UUID, by: str, **detail: Any) -> None:
    await store.set_plan_state(conn, item_id, started_at=datetime.now(UTC), started_by=by)
    await events.record(conn, item_id, "started", by, detail)


async def approve_plan(item_id: uuid.UUID, by: str) -> bool:
    """Record the plan approved at its current version. Returns False when a current approval
    already stands; a stale one is replaced."""
    async with get_pool().acquire() as conn, conn.transaction():
        st = await _locked_state(conn, item_id)
        if st["plan_approved_at"] and not st["plan_changed"]:
            return False
        await _approve(conn, item_id, st, by, replaces_stale=bool(st["plan_approved_at"]))
    return True


async def start(item_id: uuid.UUID, by: str) -> bool:
    """Record the card started. Refused without a current approval; False when already started."""
    async with get_pool().acquire() as conn, conn.transaction():
        st = await _locked_state(conn, item_id)
        if st["started_at"]:
            return False
        if not st["plan_approved_at"]:
            raise BacklogError("the plan is not approved; approve it first")
        if st["plan_changed"]:
            raise BacklogError("the plan section changed since approval; approve it again")
        await _start(conn, item_id, by)
    return True


async def withdraw(item_id: uuid.UUID, by: str, step: str) -> bool:
    """Undo a start, or an approval and the start that rests on it. Only the owner can."""
    if by != events.OWNER:
        raise Forbidden("only pryce can withdraw an approval or a start")
    async with get_pool().acquire() as conn, conn.transaction():
        st = await _locked_state(conn, item_id)
        held = "plan_approved_at" if step == "approval" else "started_at"
        if not st[held]:
            return False
        clear: dict[str, Any] = {}
        if st["started_at"]:
            clear |= {"started_at": None, "started_by": None}
        if step == "approval":
            clear |= dict.fromkeys(
                ("plan_approved_at", "plan_approved_by", "plan_approved_version")
            )
        await store.set_plan_state(conn, item_id, **clear)
        if "started_at" in clear:
            await events.record(conn, item_id, "start_withdrawn", by, {})
        if "plan_approved_at" in clear:
            await events.record(conn, item_id, "approval_withdrawn", by, {})
    return True


async def _record_worker_job_start(item_id: uuid.UUID, job_id: str) -> None:
    """The old one-click approve is Pryce approving and starting at once, so it fills whichever
    of the two the card still lacks."""
    async with get_pool().acquire() as conn, conn.transaction():
        await store.lock_status(conn, item_id)
        st = await events.state(item_id, conn)
        if not st["plan_approved_at"]:
            await _approve(conn, item_id, st, events.OWNER, via="approve")
        if not st["started_at"]:
            await _start(conn, item_id, events.OWNER, via="approve", job_id=job_id)


async def _pr_state(repo: str, number: int) -> dict[str, Any] | None:
    owner, name = repo.split("/", 1)
    try:
        resp = await get_client().get(
            f"{settings.gateway_url}/github/repos/{owner}/{name}/pulls/{number}",
            headers={"X-API-Key": settings.gateway_api_key},
            timeout=30.0,
        )
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPError:
        logger.warning("PR state check failed for %s#%s", repo, number, exc_info=True)
        return None


async def sync_pr_states() -> None:
    """Approved items follow their PR: merged -> done; closed unmerged -> back to new (with a note
    saying so), so it shows up in Open again for Pryce to retry or dismiss."""
    for item in await store.list_items(view="done"):
        meta = item.get("job_meta") or {}
        repo = (item.get("job_params") or {}).get("repo")
        if item["status"] != "approved" or not meta.get("pr_number") or not repo:
            continue
        pr = await _pr_state(repo, int(meta["pr_number"]))
        if pr is None or pr.get("state") != "closed":
            continue
        if pr.get("merged"):
            await store.update_item(item["id"], status="done")
            logger.info("Backlog item %s done: PR #%s merged", item["id"], meta["pr_number"])
        else:
            note = f"PR #{meta['pr_number']} was closed without merging."
            if item.get("note"):
                note = f"{item['note']}\n{note}"
            await store.update_item(item["id"], status="new", note=note)
            logger.info("Backlog item %s reopened: PR #%s closed", item["id"], meta["pr_number"])
