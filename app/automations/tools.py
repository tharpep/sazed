"""Handlers for Sazed's automation chat tools (INTERNAL tools; definitions live in agent/tools.py).

Chat-created automations are always sazed_prompt actions: anything the prompt asks for later
runs through the normal approval gate, so a conversation (or injected content in one) can't
schedule an unattended sensitive action.
"""

import json
import uuid
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from app.automations import runner, store
from app.automations.models import (
    DEFAULT_TIMEZONE,
    AutomationCreate,
    AutomationUpdate,
    CronSchedule,
    JournalOutput,
    Outputs,
    PromptAction,
    PushoverOutput,
)

AUTOMATION_TOOL_NAMES = frozenset(
    {
        "list_automations",
        "create_automation",
        "update_automation",
        "delete_automation",
        "run_automation",
    }
)


def _when(moment: datetime | None, tz: str) -> str | None:
    if moment is None:
        return None
    return moment.astimezone(ZoneInfo(tz)).strftime("%a %b %d, %I:%M %p %Z")


def _summary(row: dict[str, Any]) -> dict[str, Any]:
    tz = row["trigger"].get("timezone", DEFAULT_TIMEZONE)
    outputs = row["outputs"] or {}
    upcoming = (
        CronSchedule(**row["trigger"]).upcoming(datetime.now(UTC), 3) if row["enabled"] else []
    )
    return {
        "id": str(row["id"]),
        "name": row["name"],
        "enabled": row["enabled"],
        "cron": row["trigger"]["cron"],
        "timezone": tz,
        "next_runs": [_when(t, tz) for t in upcoming],
        "prompt": (
            row["action"].get("prompt")
            or f"{row['action'].get('method')} {row['action'].get('path')}"
        )[:300],
        "notify": bool(outputs.get("pushover")),
        "journal": bool(outputs.get("journal")),
        "skip_if_nothing": bool(outputs.get("skip_if_nothing")),
        "last_status": row.get("last_status"),
    }


def _outputs(notify: bool, journal: bool, skip_if_nothing: bool, current: dict | None = None):
    """Build outputs from on/off flags, keeping any existing title/category settings."""
    current = current or {}
    return Outputs(
        pushover=PushoverOutput(**(current.get("pushover") or {})) if notify else None,
        journal=JournalOutput(**(current.get("journal") or {})) if journal else None,
        skip_if_nothing=skip_if_nothing,
    )


async def handle(name: str, args: dict[str, Any]) -> str:
    """Run one automation tool; returns JSON (or a plain error string) for the model."""
    try:
        if name == "list_automations":
            rows = await store.list_automations()
            return json.dumps([_summary(r) for r in rows]) if rows else "No automations yet."

        if name == "create_automation":
            body = AutomationCreate(
                name=args["name"],
                description=args.get("description", ""),
                enabled=args.get("enabled", True),
                trigger=CronSchedule(
                    cron=args["cron"], timezone=args.get("timezone") or DEFAULT_TIMEZONE
                ),
                action=PromptAction(prompt=args["prompt"]),
                outputs=_outputs(
                    args.get("notify", True),
                    args.get("journal", False),
                    args.get("skip_if_nothing", False),
                ),
                created_by="sazed",
            )
            row = await store.create_automation(body)
            return json.dumps({"created": _summary({**row, "last_status": None})})

        automation_id = uuid.UUID(str(args["automation_id"]))
        current = await store.get_automation(automation_id)
        if current is None:
            return "Error: no automation with that id. Call list_automations first."

        if name == "update_automation":
            changes: dict[str, Any] = {}
            for key in ("name", "description", "enabled"):
                if key in args:
                    changes[key] = args[key]
            if "cron" in args or "timezone" in args:
                changes["trigger"] = CronSchedule(
                    cron=args.get("cron", current["trigger"]["cron"]),
                    timezone=args.get("timezone", current["trigger"].get("timezone")),
                )
            if "prompt" in args:
                if current["action"]["type"] != "sazed_prompt":
                    return (
                        "Error: this automation calls the gateway directly; edit it in the console."
                    )
                changes["action"] = PromptAction(prompt=args["prompt"])
            if {"notify", "journal", "skip_if_nothing"} & args.keys():
                cur = current["outputs"] or {}
                changes["outputs"] = _outputs(
                    args.get("notify", bool(cur.get("pushover"))),
                    args.get("journal", bool(cur.get("journal"))),
                    args.get("skip_if_nothing", bool(cur.get("skip_if_nothing"))),
                    current=cur,
                )
            row = await store.update_automation(automation_id, AutomationUpdate(**changes))
            return json.dumps({"updated": _summary({**row, "last_status": None})})

        if name == "delete_automation":
            await store.delete_automation(automation_id)
            return f"Deleted automation '{current['name']}'."

        if name == "run_automation":
            run_id = await runner.trigger_run(current, "manual")
            if run_id is None:
                return f"'{current['name']}' is already running."
            return (
                f"Started '{current['name']}' (run {run_id}). Results arrive via its outputs "
                "and show on the console's Automations page."
            )
    except ValidationError as e:
        return f"Error: {e.errors()[0]['msg']}"
    except (KeyError, ValueError) as e:
        return f"Error: {e}"
    return f"Unknown automation tool: {name}"
