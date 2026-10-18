"""Handlers for Sazed's backlog tools (INTERNAL tools; definitions live in agent/tools.py).

Sazed can read the backlog and file items, but never approves or dismisses them: those are
Pryce's calls, made in the console.
"""

import json
from typing import Any

from app.backlog import service, store

BACKLOG_TOOL_NAMES = frozenset({"list_backlog", "add_backlog_item"})


def _brief(item: dict[str, Any]) -> dict[str, Any]:
    out = {
        "id": str(item["id"]),
        "type": item["type"],
        "status": item["status"],
        "title": item["title"],
        "repos": item.get("repos") or [],
        "created": item["created_at"].date().isoformat(),
    }
    for key in ("summary", "effort", "impact", "rank", "note"):
        if item.get(key):
            out[key] = item[key]
    if item.get("job_meta") and item["job_meta"].get("pr_url"):
        out["pr_url"] = item["job_meta"]["pr_url"]
    return out


async def handle(name: str, args: dict[str, Any]) -> str:
    try:
        if name == "list_backlog":
            items = await store.list_items(args.get("type"), args.get("view") or "open", 50)
            return json.dumps({"count": len(items), "items": [_brief(i) for i in items]})

        if name == "add_backlog_item":
            if args.get("type") not in store.TYPES:
                return f"Error: type must be one of {', '.join(store.TYPES)}"
            item, duplicate = await service.add_item(
                type=args["type"],
                title=args["title"],
                source="chat",
                summary=args.get("summary") or "",
                why=args.get("why") or "",
                repos=args.get("repos") or [],
            )
            if duplicate:
                return json.dumps(
                    {
                        "added": False,
                        "reason": "already recorded",
                        "existing": {k: str(v) for k, v in duplicate.items()},
                    }
                )
            return json.dumps({"added": True, "item": _brief(item)})
    except (KeyError, ValueError) as e:
        return f"Error: {e}"
    return f"Unknown backlog tool: {name}"
