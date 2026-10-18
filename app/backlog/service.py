"""Backlog rules: dedupe on add, and approval turning an item into a change job."""

import logging
import uuid
from typing import Any

import httpx

from app.backlog import store
from app.config import settings
from app.http_client import get_client
from app.jobs import service as jobs

logger = logging.getLogger(__name__)


class BacklogError(ValueError):
    """An action the item's state doesn't allow (approving a dismissed item, ...)."""


async def add_item(
    *, type: str, title: str, source: str, **fields: Any
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Returns (new item, None), or (None, the existing item) when it's already recorded."""
    duplicate = await store.find_duplicate(title)
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
    return await store.update_item(
        item_id, status="approved", job_id=job["id"], note=note.strip() or None
    )


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
