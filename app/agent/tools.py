"""Tool registry — Anthropic tool schemas and executor for all gateway endpoints."""

import json
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.config import settings


@dataclass
class ToolDef:
    name: str
    description: str
    input_schema: dict
    method: str                     # GET | POST | PATCH | DELETE | INTERNAL
    endpoint: str                   # gateway path, e.g. "/calendar/events/{event_id}"
    path_params: list[str] = field(default_factory=list)


TOOLS: list[ToolDef] = [
    # -------------------------------------------------------------------------
    # Calendar
    # -------------------------------------------------------------------------
    ToolDef(
        name="get_today",
        description="Get today's calendar events.",
        input_schema={"type": "object", "properties": {}},
        method="GET",
        endpoint="/calendar/today",
    ),
    ToolDef(
        name="get_events",
        description="Get calendar events for the next N days.",
        input_schema={
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "Number of days to look ahead (1–30). Defaults to 7.",
                },
            },
        },
        method="GET",
        endpoint="/calendar/events",
    ),
    ToolDef(
        name="check_availability",
        description="Get busy time slots for the primary calendar.",
        input_schema={
            "type": "object",
            "properties": {
                "date": {
                    "type": "string",
                    "description": "Start date in YYYY-MM-DD format. Defaults to today.",
                },
                "days": {
                    "type": "integer",
                    "description": "Number of days to check (1–7). Defaults to 1.",
                },
            },
        },
        method="GET",
        endpoint="/calendar/availability",
    ),
    ToolDef(
        name="create_event",
        description="Create a new calendar event.",
        input_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Event title."},
                "start": {
                    "type": "string",
                    "description": "Start in ISO 8601 format, e.g. '2026-02-20T14:00:00'. Use YYYY-MM-DD for all-day events.",
                },
                "end": {
                    "type": "string",
                    "description": "End in ISO 8601 format.",
                },
                "all_day": {
                    "type": "boolean",
                    "description": "True for all-day events.",
                },
                "location": {"type": "string"},
                "description": {"type": "string"},
                "timezone": {
                    "type": "string",
                    "description": "Timezone string, e.g. 'America/New_York'. Defaults to America/New_York.",
                },
            },
            "required": ["title", "start", "end"],
        },
        method="POST",
        endpoint="/calendar/events",
    ),
    ToolDef(
        name="update_event",
        description="Update an existing calendar event. Only provide fields to change.",
        input_schema={
            "type": "object",
            "properties": {
                "event_id": {"type": "string", "description": "The event ID to update."},
                "title": {"type": "string"},
                "start": {"type": "string", "description": "ISO 8601 datetime."},
                "end": {"type": "string", "description": "ISO 8601 datetime."},
                "location": {"type": "string"},
                "description": {"type": "string"},
                "timezone": {"type": "string"},
            },
            "required": ["event_id"],
        },
        method="PATCH",
        endpoint="/calendar/events/{event_id}",
        path_params=["event_id"],
    ),
    ToolDef(
        name="delete_event",
        description="Delete a calendar event.",
        input_schema={
            "type": "object",
            "properties": {
                "event_id": {"type": "string", "description": "The event ID to delete."},
            },
            "required": ["event_id"],
        },
        method="DELETE",
        endpoint="/calendar/events/{event_id}",
        path_params=["event_id"],
    ),

    # -------------------------------------------------------------------------
    # Tasks
    # -------------------------------------------------------------------------
    ToolDef(
        name="get_upcoming_tasks",
        description="Get upcoming tasks from all task lists (General, Purdue, Mesh).",
        input_schema={
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "Days to look ahead (1–30). Defaults to 7.",
                },
            },
        },
        method="GET",
        endpoint="/tasks/upcoming",
    ),
    ToolDef(
        name="get_task_lists",
        description="Get all task lists with their IDs. Call this before create_task or update_task to find the list_id.",
        input_schema={"type": "object", "properties": {}},
        method="GET",
        endpoint="/tasks/lists",
    ),
    ToolDef(
        name="create_task",
        description="Create a new task in a specific list.",
        input_schema={
            "type": "object",
            "properties": {
                "list_id": {"type": "string", "description": "The task list ID from get_task_lists."},
                "title": {"type": "string", "description": "Task title."},
                "notes": {"type": "string", "description": "Task notes or description."},
                "due": {
                    "type": "string",
                    "description": "Due date in RFC 3339 format, e.g. '2026-02-20T00:00:00.000Z'.",
                },
            },
            "required": ["list_id", "title"],
        },
        method="POST",
        endpoint="/tasks/lists/{list_id}/tasks",
        path_params=["list_id"],
    ),
    ToolDef(
        name="update_task",
        description="Update a task — title, notes, due date, or mark as completed.",
        input_schema={
            "type": "object",
            "properties": {
                "list_id": {"type": "string", "description": "The task list ID."},
                "task_id": {"type": "string", "description": "The task ID."},
                "title": {"type": "string"},
                "notes": {"type": "string"},
                "due": {"type": "string", "description": "RFC 3339 timestamp."},
                "status": {
                    "type": "string",
                    "enum": ["needsAction", "completed"],
                    "description": "Task completion status.",
                },
            },
            "required": ["list_id", "task_id"],
        },
        method="PATCH",
        endpoint="/tasks/lists/{list_id}/tasks/{task_id}",
        path_params=["list_id", "task_id"],
    ),
    ToolDef(
        name="delete_task",
        description="Delete a task.",
        input_schema={
            "type": "object",
            "properties": {
                "list_id": {"type": "string", "description": "The task list ID."},
                "task_id": {"type": "string", "description": "The task ID."},
            },
            "required": ["list_id", "task_id"],
        },
        method="DELETE",
        endpoint="/tasks/lists/{list_id}/tasks/{task_id}",
        path_params=["list_id", "task_id"],
    ),

    # -------------------------------------------------------------------------
    # Email
    # -------------------------------------------------------------------------
    ToolDef(
        name="get_recent_emails",
        description="Get recent emails from the primary inbox.",
        input_schema={
            "type": "object",
            "properties": {
                "hours": {
                    "type": "integer",
                    "description": "How many hours back to look (1–168). Defaults to 24.",
                },
            },
        },
        method="GET",
        endpoint="/email/recent",
    ),
    ToolDef(
        name="get_unread_emails",
        description="Get unread emails from the primary inbox.",
        input_schema={
            "type": "object",
            "properties": {
                "max_results": {
                    "type": "integer",
                    "description": "Max emails to return (1–50). Defaults to 20.",
                },
            },
        },
        method="GET",
        endpoint="/email/unread",
    ),
    ToolDef(
        name="search_emails",
        description="Search emails using Gmail query syntax, e.g. 'from:alice subject:meeting'.",
        input_schema={
            "type": "object",
            "properties": {
                "q": {"type": "string", "description": "Gmail search query."},
                "max_results": {
                    "type": "integer",
                    "description": "Max emails to return (1–50). Defaults to 20.",
                },
            },
            "required": ["q"],
        },
        method="GET",
        endpoint="/email/search",
    ),
    ToolDef(
        name="get_email",
        description="Get the full content of a specific email by ID.",
        input_schema={
            "type": "object",
            "properties": {
                "message_id": {"type": "string", "description": "The email message ID."},
            },
            "required": ["message_id"],
        },
        method="GET",
        endpoint="/email/messages/{message_id}",
        path_params=["message_id"],
    ),
    ToolDef(
        name="draft_email",
        description="Save an email as a draft in Gmail.",
        input_schema={
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "Recipient email address."},
                "subject": {"type": "string", "description": "Email subject."},
                "body": {"type": "string", "description": "Email body text."},
                "cc": {"type": "string", "description": "CC email address."},
            },
            "required": ["to", "subject", "body"],
        },
        method="POST",
        endpoint="/email/draft",
    ),

    # -------------------------------------------------------------------------
    # Notifications
    # -------------------------------------------------------------------------
    ToolDef(
        name="send_notification",
        description="Send a push notification via Pushover.",
        input_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Notification title."},
                "message": {"type": "string", "description": "Notification body."},
                "priority": {
                    "type": "integer",
                    "enum": [-2, -1, 0, 1],
                    "description": "-2=silent, -1=quiet, 0=normal, 1=high. Defaults to 0.",
                },
                "url": {"type": "string", "description": "Optional URL to include."},
                "url_title": {"type": "string", "description": "Display text for the URL."},
            },
            "required": ["title", "message"],
        },
        method="POST",
        endpoint="/notify",
    ),

    # -------------------------------------------------------------------------
    # Knowledge Base (proxied via api-gateway → knowledge-base service)
    # -------------------------------------------------------------------------
    ToolDef(
        name="search_knowledge_base",
        description=(
            "Search personal knowledge base documents. "
            "Use category filters to scope the search to relevant sources."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query."},
                "categories": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": [
                            "general",
                            "projects",
                            "purdue",
                            "career",
                            "reference",
                        ],
                    },
                    "description": "Limit search to specific KB subfolder categories. Omit to search all.",
                },
                "top_k": {
                    "type": "integer",
                    "description": "Number of results to return. Defaults to 10.",
                },
            },
            "required": ["query"],
        },
        method="POST",
        endpoint="/kb/search",
    ),

    # -------------------------------------------------------------------------
    # Memory (internal — does not call the gateway)
    # -------------------------------------------------------------------------
    ToolDef(
        name="memory_update",
        description=(
            "Store or update a fact about the user. "
            "Call this when the user explicitly tells you something to remember, "
            "e.g. 'remember that I prefer dark mode'."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "fact_type": {
                    "type": "string",
                    "enum": ["personal", "preference", "project", "instruction", "relationship"],
                    "description": "Category of the fact.",
                },
                "key": {
                    "type": "string",
                    "description": "Short identifier for the fact, e.g. 'primary_language'.",
                },
                "value": {
                    "type": "string",
                    "description": "The fact value, e.g. 'Python'.",
                },
            },
            "required": ["fact_type", "key", "value"],
        },
        method="INTERNAL",
        endpoint="",
    ),
]

# ---------------------------------------------------------------------------
# Index + public helpers
# ---------------------------------------------------------------------------

_tool_index: dict[str, ToolDef] = {t.name: t for t in TOOLS}


def get_tool_schemas() -> list[dict]:
    """Return tool schemas in the format expected by the Anthropic messages API."""
    return [
        {
            "name": t.name,
            "description": t.description,
            "input_schema": t.input_schema,
        }
        for t in TOOLS
    ]


async def execute_tool(name: str, args: dict[str, Any]) -> str:
    """Dispatch a tool call and return the result as a string for the LLM."""
    tool = _tool_index.get(name)
    if tool is None:
        return f"Unknown tool: {name}"

    if tool.method == "INTERNAL":
        return await _execute_internal(name, args)

    # Interpolate path params, keeping remaining args for query/body
    endpoint = tool.endpoint
    remaining = dict(args)
    for param in tool.path_params:
        val = remaining.pop(param, None)
        if val is None:
            return f"Missing required path parameter: {param}"
        endpoint = endpoint.replace(f"{{{param}}}", str(val))

    url = f"{settings.gateway_url}{endpoint}"
    headers = {"X-API-Key": settings.gateway_api_key}

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            if tool.method == "GET":
                params = {k: v for k, v in remaining.items() if v is not None}
                resp = await client.get(url, params=params, headers=headers)
            elif tool.method == "POST":
                resp = await client.post(url, json=remaining, headers=headers)
            elif tool.method == "PATCH":
                resp = await client.patch(url, json=remaining, headers=headers)
            elif tool.method == "DELETE":
                resp = await client.delete(url, headers=headers)
                if resp.status_code == 204:
                    return "Deleted successfully."
            else:
                return f"Unsupported method: {tool.method}"
    except httpx.TimeoutException:
        return "Request timed out."
    except httpx.RequestError as e:
        return f"Request error: {e}"

    if not resp.is_success:
        return f"Error {resp.status_code}: {resp.text}"

    try:
        return json.dumps(resp.json(), indent=2)
    except Exception:
        return resp.text


async def _execute_internal(name: str, args: dict[str, Any]) -> str:
    """Handle internal tools that don't call the gateway."""
    if name == "memory_update":
        from app.agent.memory import upsert_fact
        fact = await upsert_fact(
            fact_type=args["fact_type"],
            key=args["key"],
            value=args["value"],
            confidence=1.0,
            source="user_explicit",
        )
        return f"Remembered: [{fact['fact_type']}] {fact['key']} = {fact['value']}"

    return f"Unknown internal tool: {name}"
