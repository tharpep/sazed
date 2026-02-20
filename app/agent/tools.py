"""Tool registry — Anthropic tool schemas and executor for all gateway endpoints."""

import ipaddress
import json
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

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
                "recurrence": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "RRULE strings for recurring events, e.g. ['FREQ=WEEKLY;BYDAY=MO'] for every Monday. Omit for one-time events.",
                },
                "reminder_minutes": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "Popup reminder times in minutes before the event, e.g. [10, 60] for 10 min and 1 hour before.",
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
                "recurrence": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "RRULE strings to set or update recurrence, e.g. ['FREQ=DAILY']. Pass an empty array to remove recurrence.",
                },
                "reminder_minutes": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "Popup reminder times in minutes before the event. Replaces existing reminders.",
                },
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

    ToolDef(
        name="search_events",
        description="Search calendar events by keyword across all time. Use when you need to find a specific event without knowing its date.",
        input_schema={
            "type": "object",
            "properties": {
                "q": {
                    "type": "string",
                    "description": "Keyword to search in event titles, descriptions, and locations.",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Max events to return (1–50). Defaults to 10.",
                },
            },
            "required": ["q"],
        },
        method="GET",
        endpoint="/calendar/events/search",
    ),

    # -------------------------------------------------------------------------
    # Tasks
    # -------------------------------------------------------------------------
    ToolDef(
        name="get_task_lists",
        description="Get all task lists with their IDs and names.",
        input_schema={"type": "object", "properties": {}},
        method="GET",
        endpoint="/tasks/lists",
    ),
    ToolDef(
        name="get_tasks",
        description="Get tasks from a specific task list. Returns all non-completed tasks by default.",
        input_schema={
            "type": "object",
            "properties": {
                "list_id": {
                    "type": "string",
                    "description": "The task list ID. Get available lists with get_task_lists.",
                },
                "include_completed": {
                    "type": "boolean",
                    "description": "Include completed tasks. Defaults to false.",
                },
            },
            "required": ["list_id"],
        },
        method="GET",
        endpoint="/tasks/lists/{list_id}/tasks",
        path_params=["list_id"],
    ),
    ToolDef(
        name="create_task_list",
        description="Create a new Google Tasks task list.",
        input_schema={
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Task list name.",
                },
            },
            "required": ["title"],
        },
        method="POST",
        endpoint="/tasks/lists",
    ),
    ToolDef(
        name="rename_task_list",
        description="Rename an existing task list.",
        input_schema={
            "type": "object",
            "properties": {
                "list_id": {
                    "type": "string",
                    "description": "The task list ID.",
                },
                "title": {
                    "type": "string",
                    "description": "New task list name.",
                },
            },
            "required": ["list_id", "title"],
        },
        method="PATCH",
        endpoint="/tasks/lists/{list_id}",
        path_params=["list_id"],
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
                    "description": "Task completion status. Set to 'completed' to mark a task as done, 'needsAction' to reopen it.",
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
        name="list_emails",
        description="List emails from the primary inbox. Filter by unread status and/or recency.",
        input_schema={
            "type": "object",
            "properties": {
                "unread_only": {
                    "type": "boolean",
                    "description": "If true, return only unread emails. Defaults to false.",
                },
                "hours": {
                    "type": "integer",
                    "description": "Limit to emails received within the last N hours (1–168). Omit for no time filter.",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Max emails to return (1–50). Defaults to 20.",
                },
            },
        },
        method="GET",
        endpoint="/email",
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
        description="Save an email as a draft in Gmail. Does not send — user must send from Gmail.",
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
    # KB write workflow: use create_file to place a file in the correct Drive
    # KB subfolder, then call sync_kb to index it. The KB is Drive-backed so
    # sync is the only write path — direct ingest would be wiped on next sync.
    #
    # Drive KB subfolder IDs per category are not yet configured. Use list_files
    # with a folder search to locate the right subfolder before creating a file.
    # TODO: expose KB_FOLDER_IDS as an env var / config so the agent can look
    #       them up without a list_files round-trip each time.
    ToolDef(
        name="list_kb_sources",
        description=(
            "List all documents currently indexed in the knowledge base. "
            "Returns each source's file_id, filename, category, chunk count, and sync status. "
            "Use file_id with delete_kb_source to remove a specific entry."
        ),
        input_schema={"type": "object", "properties": {}},
        method="GET",
        endpoint="/kb/sources",
    ),
    ToolDef(
        name="delete_kb_source",
        description=(
            "Remove a document from the knowledge base by its source ID. "
            "This deletes the indexed chunks only — the Drive file is not touched. "
            "Use list_kb_sources first to find the file_id."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "source_id": {
                    "type": "string",
                    "description": "The file_id from list_kb_sources.",
                },
            },
            "required": ["source_id"],
        },
        method="DELETE",
        endpoint="/kb/files/{source_id}",
        path_params=["source_id"],
    ),
    ToolDef(
        name="sync_kb",
        description=(
            "Sync the knowledge base with Google Drive. "
            "Picks up new and modified files added since the last sync. "
            "Call this after using create_file to place a new document in a Drive KB subfolder."
        ),
        input_schema={"type": "object", "properties": {}},
        method="POST",
        endpoint="/kb/sync",
    ),

    # -------------------------------------------------------------------------
    # Web Search (Tavily)
    # -------------------------------------------------------------------------
    ToolDef(
        name="web_search",
        description="Search the web for current information. Use when the knowledge base doesn't have the answer or the topic requires up-to-date data.",
        input_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query.",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Number of results to return (1–10). Defaults to 5.",
                },
                "search_depth": {
                    "type": "string",
                    "enum": ["basic", "advanced"],
                    "description": "'basic' is faster, 'advanced' does deeper extraction. Defaults to 'basic'.",
                },
            },
            "required": ["query"],
        },
        method="POST",
        endpoint="/search/web",
    ),
    ToolDef(
        name="fetch_url",
        description="Fetch and extract the readable text content from a specific URL. Use when you have a URL and need its full content.",
        input_schema={
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The URL to fetch.",
                },
            },
            "required": ["url"],
        },
        method="POST",
        endpoint="/search/web/fetch",
    ),

    # -------------------------------------------------------------------------
    # Storage (Google Drive)
    # -------------------------------------------------------------------------
    ToolDef(
        name="list_files",
        description="List files in Google Drive. Filter by folder or search query. Use to find a file ID before reading or modifying it.",
        input_schema={
            "type": "object",
            "properties": {
                "folder_id": {
                    "type": "string",
                    "description": "Limit results to a specific Drive folder ID.",
                },
                "query": {
                    "type": "string",
                    "description": "Drive search query, e.g. 'name contains \"resume\"'.",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Max files to return (1–50). Defaults to 20.",
                },
            },
        },
        method="GET",
        endpoint="/storage/files",
    ),
    ToolDef(
        name="list_folders",
        description="List Drive folders. Use parent_id to browse into a specific folder, or query to search by name. Use this to find a folder ID before creating files or subfolders inside it.",
        input_schema={
            "type": "object",
            "properties": {
                "parent_id": {
                    "type": "string",
                    "description": "Scope results to a specific parent folder ID.",
                },
                "query": {
                    "type": "string",
                    "description": "Drive name filter, e.g. 'name contains \"Projects\"'.",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Max folders to return (1–50). Defaults to 20.",
                },
            },
        },
        method="GET",
        endpoint="/storage/folders",
    ),
    ToolDef(
        name="create_folder",
        description="Create a new folder in Google Drive, optionally nested inside a parent folder.",
        input_schema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Folder name.",
                },
                "parent_id": {
                    "type": "string",
                    "description": "Parent folder ID. Defaults to Drive root if omitted.",
                },
            },
            "required": ["name"],
        },
        method="POST",
        endpoint="/storage/folders",
    ),
    ToolDef(
        name="get_file",
        description="Fetch the full text content of a Google Drive file by ID. Works with text files, Markdown, CSV, JSON, Google Docs, and PDFs.",
        input_schema={
            "type": "object",
            "properties": {
                "file_id": {
                    "type": "string",
                    "description": "The Drive file ID.",
                },
            },
            "required": ["file_id"],
        },
        method="GET",
        endpoint="/storage/files/{file_id}/content",
        path_params=["file_id"],
    ),
    ToolDef(
        name="create_file",
        description="Create a new file in Google Drive with the given content. Pass mime_type='application/vnd.google-apps.document' to create a native Google Doc.",
        input_schema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "File name including extension, e.g. 'meeting-notes.md'.",
                },
                "content": {
                    "type": "string",
                    "description": "File content as plain text.",
                },
                "folder_id": {
                    "type": "string",
                    "description": "Parent folder ID. Defaults to Drive root if omitted.",
                },
                "mime_type": {
                    "type": "string",
                    "description": "MIME type, e.g. 'text/plain', 'text/markdown', 'text/csv', 'application/vnd.google-apps.document'. Defaults to text/plain.",
                },
            },
            "required": ["name", "content"],
        },
        method="POST",
        endpoint="/storage/files",
    ),
    ToolDef(
        name="update_file",
        description="Overwrite the content of an existing Google Drive text file. Replaces the entire file content.",
        input_schema={
            "type": "object",
            "properties": {
                "file_id": {
                    "type": "string",
                    "description": "The Drive file ID.",
                },
                "content": {
                    "type": "string",
                    "description": "New file content. Replaces existing content entirely.",
                },
            },
            "required": ["file_id", "content"],
        },
        method="PUT",
        endpoint="/storage/files/{file_id}",
        path_params=["file_id"],
    ),
    ToolDef(
        name="append_to_file",
        description="Append text to an existing Google Drive file or Google Doc without overwriting its current content.",
        input_schema={
            "type": "object",
            "properties": {
                "file_id": {
                    "type": "string",
                    "description": "The Drive file ID.",
                },
                "content": {
                    "type": "string",
                    "description": "Text to append.",
                },
                "separator": {
                    "type": "string",
                    "description": "String inserted between existing content and new content. Defaults to two newlines.",
                },
            },
            "required": ["file_id", "content"],
        },
        method="POST",
        endpoint="/storage/files/{file_id}/append",
        path_params=["file_id"],
    ),
    ToolDef(
        name="delete_file",
        description="Move a Google Drive file to trash.",
        input_schema={
            "type": "object",
            "properties": {
                "file_id": {
                    "type": "string",
                    "description": "The Drive file ID.",
                },
            },
            "required": ["file_id"],
        },
        method="DELETE",
        endpoint="/storage/files/{file_id}",
        path_params=["file_id"],
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
    schemas = [
        {
            "name": t.name,
            "description": t.description,
            "input_schema": t.input_schema,
        }
        for t in TOOLS
    ]
    schemas[-1]["cache_control"] = {"type": "ephemeral"}
    return schemas


_SSRF_BLOCKED_HOSTS = {"localhost", "metadata.google.internal"}


def _check_ssrf(url: str) -> str | None:
    """Return an error string if the URL targets an internal/private resource, else None."""
    try:
        parsed = urlparse(url)
    except Exception:
        return "Invalid URL."

    if parsed.scheme not in ("http", "https"):
        return "Only http and https URLs are allowed."

    host = (parsed.hostname or "").lower()

    if host in _SSRF_BLOCKED_HOSTS:
        return f"Blocked: '{host}' is not an allowed host."

    try:
        addr = ipaddress.ip_address(host)
        if any([addr.is_private, addr.is_loopback, addr.is_link_local, addr.is_reserved]):
            return "Blocked: private and internal IP addresses are not allowed."
    except ValueError:
        pass  # Hostname, not a literal IP — allowed

    return None


async def execute_tool(name: str, args: dict[str, Any]) -> str:
    """Dispatch a tool call and return the result as a string for the LLM."""
    tool = _tool_index.get(name)
    if tool is None:
        return f"Unknown tool: {name}"

    if tool.method == "INTERNAL":
        return await _execute_internal(name, args)

    # SSRF guard — validate any URL argument before forwarding to the gateway
    if "url" in args:
        ssrf_err = _check_ssrf(str(args["url"]))
        if ssrf_err:
            return f"Blocked: {ssrf_err}"

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
            elif tool.method == "PUT":
                resp = await client.put(url, json=remaining, headers=headers)
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
