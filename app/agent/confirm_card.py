"""The card shown when a sensitive tool pauses for confirmation.

Built from the tool name and its input in code. The answer is still the user's next message
("yes" or "no"), settled by the pending_actions gate in loop.py.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

# Matches the 10-minute expiry in loop._handle_pending_actions.
PENDING_TTL = timedelta(minutes=10)


def _detail(label: str, value: Any) -> dict[str, str]:
    return {"label": label, "value": str(value)}


def _restart_container(args: dict[str, Any]) -> tuple[str, list[dict[str, str]]]:
    return f"Restart container {args.get('vmid')}", [_detail("Container", args.get("vmid"))]


def _start_container(args: dict[str, Any]) -> tuple[str, list[dict[str, str]]]:
    return f"Start container {args.get('vmid')}", [_detail("Container", args.get("vmid"))]


def _send_email(args: dict[str, Any]) -> tuple[str, list[dict[str, str]]]:
    details = [_detail("To", args.get("to")), _detail("Subject", args.get("subject"))]
    if args.get("cc"):
        details.append(_detail("Cc", args["cc"]))
    return f"Send an email to {args.get('to')}", details


def _delete_event(args: dict[str, Any]) -> tuple[str, list[dict[str, str]]]:
    return "Delete a calendar event", [_detail("Event ID", args.get("event_id"))]


_FORMATTERS = {
    "restart_container": _restart_container,
    "start_container": _start_container,
    "send_email": _send_email,
    "delete_event": _delete_event,
}


def build_confirm_card(name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
    formatter = _FORMATTERS.get(name)
    if formatter:
        title, details = formatter(tool_input)
    else:
        title = f"Run {name.replace('_', ' ')}"
        details = [_detail(key.replace("_", " ").capitalize(), v) for key, v in tool_input.items()]
    expires_at = (datetime.now(UTC) + PENDING_TTL).isoformat()
    return {
        "component": "ConfirmCard",
        "props": {"tool": name, "title": title, "details": details, "expires_at": expires_at},
    }
