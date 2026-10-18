"""Backlog rules: dedupe on add, and approval turning an item into a change job."""

import uuid
from typing import Any

from app.backlog import store
from app.jobs import service as jobs


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
    parts = [f"Implement this backlog item: {item['title']}"]
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
