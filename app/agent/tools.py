"""Tool registry — Anthropic tool schemas and executor for all gateway endpoints."""

import ipaddress
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlparse

import cachetools
import httpx

from app.agent.memory import upsert_fact
from app.config import settings


@dataclass
class ToolResult:
    content: str       # string sent to the LLM as tool result
    status: str        # "success" | "error"
    error: str | None  # human-readable error cause, None on success
    duration_ms: int


# ---------------------------------------------------------------------------
# Tool result cache — read-only tools, TTL + maxsize, process-scoped in-memory
# ---------------------------------------------------------------------------

_TOOL_CACHE: cachetools.TTLCache = cachetools.TTLCache(maxsize=256, ttl=settings.tool_cache_ttl_seconds)

_CACHEABLE_TOOLS: frozenset[str] = frozenset({
    "get_events", "check_availability", "search_events",
    "get_task_lists", "get_tasks",
    "list_emails", "search_emails", "get_email",
    "search_knowledge_base", "get_kb_index", "list_kb_sources",
    "list_files", "list_folders", "get_file_info", "read_file",
    "web_search", "fetch_url",
    "list_repos", "get_repo", "list_issues", "get_issue",
    "list_prs", "get_pr", "get_github_file", "list_commits",
    "list_branches", "list_releases", "get_latest_release",
    "get_subscriptions", "get_budget", "get_income",
    "get_upcoming_bills", "get_monthly_summary",
    "get_spreadsheet_info", "read_sheet",
})


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
        description=(
            "Create a new task in a specific list. "
            "Also use this to set reminders — tasks with a due datetime appear in Google Calendar "
            "and fire native push notifications. For reminders, use an appropriate list "
            "(e.g. find or create a 'Reminders' list) and set a specific due datetime."
        ),
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
                            "conversations",
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
    ToolDef(
        name="get_kb_index",
        description=(
            "Get a directory of all documents in the knowledge base with one-line summaries. "
            "Use this before searching when you need to discover what topics or files exist, "
            "or when the user asks what you know about a broad subject. "
            "More efficient than searching blindly — call this first to triage, then search specific files."
        ),
        input_schema={"type": "object", "properties": {}},
        method="GET",
        endpoint="/kb/index",
    ),
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
    ToolDef(
        name="aggregate_search",
        description=(
            "Search across Reddit, Hacker News, Bluesky, and news sources simultaneously. "
            "Returns normalized results with credibility tiers, corroboration clusters, and "
            "bias signals (hedge ratio, named source count, content type, fact-check hits). "
            "Use for news, current events, research, or when you need multi-source coverage "
            "on a topic. Prefer over web_search for news, opinions, and social discourse."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query.",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Max total results across all platforms (1-50). Defaults to 25.",
                },
                "platforms": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Platforms to search: 'reddit', 'hn', 'bluesky', 'gnews', "
                        "'google_news_rss', 'rss'. Omit to search all available."
                    ),
                },
                "since": {
                    "type": "string",
                    "description": "ISO 8601 timestamp. Only return results newer than this.",
                },
            },
            "required": ["query"],
        },
        method="POST",
        endpoint="/multi-search/aggregate",
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
        name="get_file_info",
        description="Get metadata for a Drive file — name, MIME type, size, and modified time. Use this to identify a file type before reading or manipulating it.",
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
        endpoint="/storage/files/{file_id}",
        path_params=["file_id"],
    ),
    ToolDef(
        name="read_file",
        description="Fetch the full text content of a Google Drive file by ID. Works with text files, Markdown, CSV, JSON, Google Docs, PDFs, and Google Sheets (exported as CSV of the first sheet).",
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
    ToolDef(
        name="move_file",
        description="Rename and/or move a Drive file to a different folder. Provide at least one of name or folder_id.",
        input_schema={
            "type": "object",
            "properties": {
                "file_id": {
                    "type": "string",
                    "description": "The Drive file ID.",
                },
                "name": {
                    "type": "string",
                    "description": "New file name. Omit to keep the current name.",
                },
                "folder_id": {
                    "type": "string",
                    "description": "Destination folder ID. Omit to keep the current location.",
                },
            },
            "required": ["file_id"],
        },
        method="PATCH",
        endpoint="/storage/files/{file_id}",
        path_params=["file_id"],
    ),
    ToolDef(
        name="copy_file",
        description="Copy a Drive file within Drive. Optionally rename the copy or place it in a different folder.",
        input_schema={
            "type": "object",
            "properties": {
                "file_id": {
                    "type": "string",
                    "description": "The Drive file ID to copy.",
                },
                "name": {
                    "type": "string",
                    "description": "Name for the copy. Defaults to 'Copy of {original name}'.",
                },
                "folder_id": {
                    "type": "string",
                    "description": "Destination folder ID. Defaults to the same folder as the original.",
                },
            },
            "required": ["file_id"],
        },
        method="POST",
        endpoint="/storage/files/{file_id}/copy",
        path_params=["file_id"],
    ),
    ToolDef(
        name="copy_file_from_github",
        description="Fetch a file from a GitHub repository and save it directly to Google Drive.",
        input_schema={
            "type": "object",
            "properties": {
                "owner": {
                    "type": "string",
                    "description": "GitHub repo owner.",
                },
                "repo": {
                    "type": "string",
                    "description": "GitHub repository name.",
                },
                "path": {
                    "type": "string",
                    "description": "Path to the file within the repo, e.g. 'src/main.py'.",
                },
                "ref": {
                    "type": "string",
                    "description": "Branch, tag, or commit SHA. Defaults to the default branch.",
                },
                "folder_id": {
                    "type": "string",
                    "description": "Drive folder ID to save the file into. Defaults to Drive root.",
                },
                "name": {
                    "type": "string",
                    "description": "Filename to use in Drive. Defaults to the filename from the path.",
                },
                "mime_type": {
                    "type": "string",
                    "description": "MIME type for the Drive file. Defaults to text/plain.",
                },
            },
            "required": ["owner", "repo", "path"],
        },
        method="POST",
        endpoint="/storage/files/copy-from-github",
    ),

    # -------------------------------------------------------------------------
    # GitHub
    # -------------------------------------------------------------------------
    ToolDef(
        name="list_repos",
        description="List your GitHub repositories. Returns name, language, stars, open issue count, and default branch.",
        input_schema={
            "type": "object",
            "properties": {
                "sort": {
                    "type": "string",
                    "enum": ["updated", "created", "pushed", "full_name"],
                    "description": "Sort order. Defaults to 'updated'.",
                },
                "per_page": {
                    "type": "integer",
                    "description": "Max repos to return (1–100). Defaults to 30.",
                },
            },
        },
        method="GET",
        endpoint="/github/repos",
    ),
    ToolDef(
        name="get_repo",
        description="Get details for a specific GitHub repository.",
        input_schema={
            "type": "object",
            "properties": {
                "owner": {"type": "string", "description": "Repo owner. Defaults to tharpep for personal repos."},
                "repo": {"type": "string", "description": "Repository name."},
            },
            "required": ["owner", "repo"],
        },
        method="GET",
        endpoint="/github/repos/{owner}/{repo}",
        path_params=["owner", "repo"],
    ),
    ToolDef(
        name="list_issues",
        description="List issues in a GitHub repository. Pull requests are excluded.",
        input_schema={
            "type": "object",
            "properties": {
                "owner": {"type": "string", "description": "Repo owner. Use 'tharpep' for personal repos."},
                "repo": {"type": "string", "description": "Repository name."},
                "state": {
                    "type": "string",
                    "enum": ["open", "closed", "all"],
                    "description": "Filter by state. Defaults to 'open'.",
                },
                "labels": {"type": "string", "description": "Comma-separated label names to filter by."},
                "per_page": {"type": "integer", "description": "Max issues to return (1–100). Defaults to 20."},
            },
            "required": ["owner", "repo"],
        },
        method="GET",
        endpoint="/github/repos/{owner}/{repo}/issues",
        path_params=["owner", "repo"],
    ),
    ToolDef(
        name="get_issue",
        description="Get a specific GitHub issue including its full body and all comments.",
        input_schema={
            "type": "object",
            "properties": {
                "owner": {"type": "string", "description": "Repo owner."},
                "repo": {"type": "string", "description": "Repository name."},
                "number": {"type": "integer", "description": "Issue number."},
            },
            "required": ["owner", "repo", "number"],
        },
        method="GET",
        endpoint="/github/repos/{owner}/{repo}/issues/{number}",
        path_params=["owner", "repo", "number"],
    ),
    ToolDef(
        name="create_issue",
        description="Open a new issue in a GitHub repository.",
        input_schema={
            "type": "object",
            "properties": {
                "owner": {"type": "string", "description": "Repo owner."},
                "repo": {"type": "string", "description": "Repository name."},
                "title": {"type": "string", "description": "Issue title."},
                "body": {"type": "string", "description": "Issue body (Markdown supported)."},
                "labels": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Labels to apply.",
                },
            },
            "required": ["owner", "repo", "title"],
        },
        method="POST",
        endpoint="/github/repos/{owner}/{repo}/issues",
        path_params=["owner", "repo"],
    ),
    ToolDef(
        name="update_issue",
        description="Update an existing issue — edit title, body, open/close it, or change labels. Only provide fields to change.",
        input_schema={
            "type": "object",
            "properties": {
                "owner": {"type": "string", "description": "Repo owner."},
                "repo": {"type": "string", "description": "Repository name."},
                "number": {"type": "integer", "description": "Issue number."},
                "title": {"type": "string"},
                "body": {"type": "string"},
                "state": {
                    "type": "string",
                    "enum": ["open", "closed"],
                    "description": "Set to 'closed' to close the issue, 'open' to reopen it.",
                },
                "labels": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Replaces all current labels.",
                },
            },
            "required": ["owner", "repo", "number"],
        },
        method="PATCH",
        endpoint="/github/repos/{owner}/{repo}/issues/{number}",
        path_params=["owner", "repo", "number"],
    ),
    ToolDef(
        name="add_issue_comment",
        description="Add a comment to a GitHub issue.",
        input_schema={
            "type": "object",
            "properties": {
                "owner": {"type": "string", "description": "Repo owner."},
                "repo": {"type": "string", "description": "Repository name."},
                "number": {"type": "integer", "description": "Issue number."},
                "body": {"type": "string", "description": "Comment text (Markdown supported)."},
            },
            "required": ["owner", "repo", "number", "body"],
        },
        method="POST",
        endpoint="/github/repos/{owner}/{repo}/issues/{number}/comments",
        path_params=["owner", "repo", "number"],
    ),
    ToolDef(
        name="list_prs",
        description="List pull requests in a GitHub repository.",
        input_schema={
            "type": "object",
            "properties": {
                "owner": {"type": "string", "description": "Repo owner."},
                "repo": {"type": "string", "description": "Repository name."},
                "state": {
                    "type": "string",
                    "enum": ["open", "closed", "all"],
                    "description": "Filter by state. Defaults to 'open'.",
                },
                "per_page": {"type": "integer", "description": "Max PRs to return (1–100). Defaults to 20."},
            },
            "required": ["owner", "repo"],
        },
        method="GET",
        endpoint="/github/repos/{owner}/{repo}/pulls",
        path_params=["owner", "repo"],
    ),
    ToolDef(
        name="get_pr",
        description="Get details for a specific pull request including its description and branch info.",
        input_schema={
            "type": "object",
            "properties": {
                "owner": {"type": "string", "description": "Repo owner."},
                "repo": {"type": "string", "description": "Repository name."},
                "number": {"type": "integer", "description": "PR number."},
            },
            "required": ["owner", "repo", "number"],
        },
        method="GET",
        endpoint="/github/repos/{owner}/{repo}/pulls/{number}",
        path_params=["owner", "repo", "number"],
    ),
    ToolDef(
        name="add_pr_comment",
        description="Add a general comment to a pull request.",
        input_schema={
            "type": "object",
            "properties": {
                "owner": {"type": "string", "description": "Repo owner."},
                "repo": {"type": "string", "description": "Repository name."},
                "number": {"type": "integer", "description": "PR number."},
                "body": {"type": "string", "description": "Comment text (Markdown supported)."},
            },
            "required": ["owner", "repo", "number", "body"],
        },
        method="POST",
        endpoint="/github/repos/{owner}/{repo}/pulls/{number}/comments",
        path_params=["owner", "repo", "number"],
    ),
    ToolDef(
        name="create_pr",
        description="Open a new pull request. The head branch must already exist and have commits ahead of the base branch.",
        input_schema={
            "type": "object",
            "properties": {
                "owner": {"type": "string", "description": "Repo owner."},
                "repo": {"type": "string", "description": "Repository name."},
                "title": {"type": "string", "description": "PR title."},
                "head": {"type": "string", "description": "Source branch name."},
                "base": {"type": "string", "description": "Target branch name, e.g. 'main'."},
                "body": {"type": "string", "description": "PR description (Markdown supported)."},
                "draft": {"type": "boolean", "description": "Open as a draft PR. Defaults to false."},
            },
            "required": ["owner", "repo", "title", "head", "base"],
        },
        method="POST",
        endpoint="/github/repos/{owner}/{repo}/pulls",
        path_params=["owner", "repo"],
    ),
    ToolDef(
        name="search_issues",
        description="Search issues and PRs across GitHub. Append 'repo:owner/name' to the query to scope to a specific repo.",
        input_schema={
            "type": "object",
            "properties": {
                "q": {"type": "string", "description": "Search query, e.g. 'bug repo:tharpep/myrepo'."},
                "per_page": {"type": "integer", "description": "Max results (1–30). Defaults to 10."},
            },
            "required": ["q"],
        },
        method="GET",
        endpoint="/github/search/issues",
    ),
    ToolDef(
        name="get_github_file",
        description="Read a file or list a directory from a GitHub repository. Returns decoded text content for files.",
        input_schema={
            "type": "object",
            "properties": {
                "owner": {"type": "string", "description": "Repo owner."},
                "repo": {"type": "string", "description": "Repository name."},
                "path": {"type": "string", "description": "File or directory path, e.g. 'src/main.py' or 'src/'."},
                "ref": {"type": "string", "description": "Branch, tag, or commit SHA. Defaults to the default branch."},
            },
            "required": ["owner", "repo", "path"],
        },
        method="GET",
        endpoint="/github/repos/{owner}/{repo}/contents/{path}",
        path_params=["owner", "repo", "path"],
    ),
    ToolDef(
        name="search_code",
        description="Search code across GitHub repositories. Use 'repo:owner/name' to scope to a specific repo. Rate-limited to 30 requests/minute.",
        input_schema={
            "type": "object",
            "properties": {
                "q": {"type": "string", "description": "Search query, e.g. 'addClass repo:tharpep/myrepo'."},
                "per_page": {"type": "integer", "description": "Max results (1–30). Defaults to 10."},
            },
            "required": ["q"],
        },
        method="GET",
        endpoint="/github/search/code",
    ),
    ToolDef(
        name="list_commits",
        description="List commits on a GitHub repository. Filter by branch/tag (sha), author, file path, or date range.",
        input_schema={
            "type": "object",
            "properties": {
                "owner": {"type": "string", "description": "Repo owner (GitHub username or org)."},
                "repo": {"type": "string", "description": "Repository name."},
                "sha": {"type": "string", "description": "Branch, tag, or commit SHA to start listing from."},
                "author": {"type": "string", "description": "Filter by GitHub username or email address."},
                "path": {"type": "string", "description": "Only return commits that touched this file path."},
                "since": {"type": "string", "description": "ISO 8601 timestamp — only commits after this date."},
                "until": {"type": "string", "description": "ISO 8601 timestamp — only commits before this date."},
                "per_page": {"type": "integer", "description": "Max commits to return (1–100). Defaults to 20."},
            },
            "required": ["owner", "repo"],
        },
        method="GET",
        endpoint="/github/repos/{owner}/{repo}/commits",
        path_params=["owner", "repo"],
    ),
    ToolDef(
        name="get_commit",
        description="Get full details for a single commit: message, author, stats (additions/deletions), and per-file diffs.",
        input_schema={
            "type": "object",
            "properties": {
                "owner": {"type": "string", "description": "Repo owner."},
                "repo": {"type": "string", "description": "Repository name."},
                "sha": {"type": "string", "description": "Full or short commit SHA."},
            },
            "required": ["owner", "repo", "sha"],
        },
        method="GET",
        endpoint="/github/repos/{owner}/{repo}/commits/{sha}",
        path_params=["owner", "repo", "sha"],
    ),
    ToolDef(
        name="list_branches",
        description="List branches in a GitHub repository, including their HEAD SHA and whether they are protected.",
        input_schema={
            "type": "object",
            "properties": {
                "owner": {"type": "string", "description": "Repo owner."},
                "repo": {"type": "string", "description": "Repository name."},
                "per_page": {"type": "integer", "description": "Max branches to return (1–100). Defaults to 30."},
            },
            "required": ["owner", "repo"],
        },
        method="GET",
        endpoint="/github/repos/{owner}/{repo}/branches",
        path_params=["owner", "repo"],
    ),
    ToolDef(
        name="list_tags",
        description="List tags in a GitHub repository.",
        input_schema={
            "type": "object",
            "properties": {
                "owner": {"type": "string", "description": "Repo owner."},
                "repo": {"type": "string", "description": "Repository name."},
                "per_page": {"type": "integer", "description": "Max tags to return (1–100). Defaults to 30."},
            },
            "required": ["owner", "repo"],
        },
        method="GET",
        endpoint="/github/repos/{owner}/{repo}/tags",
        path_params=["owner", "repo"],
    ),
    ToolDef(
        name="list_releases",
        description="List releases in a GitHub repository, including tag, name, draft/prerelease flags, and release notes.",
        input_schema={
            "type": "object",
            "properties": {
                "owner": {"type": "string", "description": "Repo owner."},
                "repo": {"type": "string", "description": "Repository name."},
                "per_page": {"type": "integer", "description": "Max releases to return (1–100). Defaults to 10."},
            },
            "required": ["owner", "repo"],
        },
        method="GET",
        endpoint="/github/repos/{owner}/{repo}/releases",
        path_params=["owner", "repo"],
    ),
    ToolDef(
        name="get_latest_release",
        description="Get the latest published (non-draft, non-prerelease) release for a GitHub repository.",
        input_schema={
            "type": "object",
            "properties": {
                "owner": {"type": "string", "description": "Repo owner."},
                "repo": {"type": "string", "description": "Repository name."},
            },
            "required": ["owner", "repo"],
        },
        method="GET",
        endpoint="/github/repos/{owner}/{repo}/releases/latest",
        path_params=["owner", "repo"],
    ),
    ToolDef(
        name="get_pr_reviews",
        description="Get all reviews on a pull request — shows reviewer, state (APPROVED, CHANGES_REQUESTED, COMMENTED, DISMISSED), and review body.",
        input_schema={
            "type": "object",
            "properties": {
                "owner": {"type": "string", "description": "Repo owner."},
                "repo": {"type": "string", "description": "Repository name."},
                "number": {"type": "integer", "description": "Pull request number."},
            },
            "required": ["owner", "repo", "number"],
        },
        method="GET",
        endpoint="/github/repos/{owner}/{repo}/pulls/{number}/reviews",
        path_params=["owner", "repo", "number"],
    ),
    ToolDef(
        name="get_pr_files",
        description="List files changed in a pull request with additions, deletions, and patch diffs.",
        input_schema={
            "type": "object",
            "properties": {
                "owner": {"type": "string", "description": "Repo owner."},
                "repo": {"type": "string", "description": "Repository name."},
                "number": {"type": "integer", "description": "Pull request number."},
            },
            "required": ["owner", "repo", "number"],
        },
        method="GET",
        endpoint="/github/repos/{owner}/{repo}/pulls/{number}/files",
        path_params=["owner", "repo", "number"],
    ),
    ToolDef(
        name="list_contributors",
        description="List contributors to a GitHub repository sorted by commit count.",
        input_schema={
            "type": "object",
            "properties": {
                "owner": {"type": "string", "description": "Repo owner."},
                "repo": {"type": "string", "description": "Repository name."},
                "per_page": {"type": "integer", "description": "Max contributors to return (1–100). Defaults to 20."},
            },
            "required": ["owner", "repo"],
        },
        method="GET",
        endpoint="/github/repos/{owner}/{repo}/contributors",
        path_params=["owner", "repo"],
    ),
    ToolDef(
        name="compare_refs",
        description="Compare two refs (branches, tags, or SHAs) in a repository. Returns status (ahead/behind/diverged/identical), commit list, and changed files.",
        input_schema={
            "type": "object",
            "properties": {
                "owner": {"type": "string", "description": "Repo owner."},
                "repo": {"type": "string", "description": "Repository name."},
                "base": {"type": "string", "description": "Base ref (branch, tag, or SHA) to compare from."},
                "head": {"type": "string", "description": "Head ref to compare against base."},
            },
            "required": ["owner", "repo", "base", "head"],
        },
        method="GET",
        endpoint="/github/repos/{owner}/{repo}/compare",
        path_params=["owner", "repo"],
    ),

    # -------------------------------------------------------------------------
    # Google Sheets
    # -------------------------------------------------------------------------
    ToolDef(
        name="create_spreadsheet",
        description="Create a new Google Spreadsheet. Optionally place it in a specific Drive folder.",
        input_schema={
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Spreadsheet title.",
                },
                "folder_id": {
                    "type": "string",
                    "description": "Drive folder ID to place the spreadsheet in. Defaults to Drive root.",
                },
            },
            "required": ["title"],
        },
        method="POST",
        endpoint="/sheets",
    ),
    ToolDef(
        name="get_spreadsheet_info",
        description="Get spreadsheet metadata: title and all sheet tab names, IDs, and dimensions. Call this before reading or writing to confirm tab names and structure.",
        input_schema={
            "type": "object",
            "properties": {
                "spreadsheet_id": {
                    "type": "string",
                    "description": "The spreadsheet ID (from the Drive file ID or URL).",
                },
            },
            "required": ["spreadsheet_id"],
        },
        method="GET",
        endpoint="/sheets/{spreadsheet_id}",
        path_params=["spreadsheet_id"],
    ),
    ToolDef(
        name="read_sheet",
        description=(
            "Read cell values from a spreadsheet range. "
            "Range uses A1 notation, e.g. 'Sheet1!A1:D20' or just 'Sheet1' for the whole sheet. "
            "Returns a 2D array of values."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "spreadsheet_id": {
                    "type": "string",
                    "description": "The spreadsheet ID.",
                },
                "range": {
                    "type": "string",
                    "description": "A1 notation range, e.g. 'Sheet1!A1:D20' or 'Sheet1'.",
                },
            },
            "required": ["spreadsheet_id", "range"],
        },
        method="GET",
        endpoint="/sheets/{spreadsheet_id}/values/{range}",
        path_params=["spreadsheet_id", "range"],
    ),
    ToolDef(
        name="write_sheet",
        description=(
            "Overwrite values in a spreadsheet range. Existing values in the range are replaced. "
            "The range must exactly match the data: for N rows × M columns use e.g. 'Tab!A1:CM' — "
            "extra cells in a larger range are cleared; rows beyond the range are silently dropped. "
            "Tab names that contain spaces must be single-quoted: e.g. \"'My Sheet'!A1:C3\". "
            "values is a 2D array; each inner array is one row, e.g. [['Name', 'Amount'], ['Coffee', '4.50']]. "
            "Call get_spreadsheet_info first if you don't already know the exact tab name."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "spreadsheet_id": {
                    "type": "string",
                    "description": "The spreadsheet ID.",
                },
                "range": {
                    "type": "string",
                    "description": "A1 notation range, e.g. 'Sheet1!A1:C3'. Single-quote tab names with spaces: \"'My Sheet'!A1:C3\".",
                },
                "values": {
                    "type": "array",
                    "items": {
                        "type": "array",
                        "items": {"type": ["string", "number", "boolean", "null"]},
                    },
                    "description": "2D array of values to write. Each inner array is one row.",
                },
                "value_input_option": {
                    "type": "string",
                    "enum": ["USER_ENTERED", "RAW"],
                    "description": "USER_ENTERED parses values as if typed by a user (formulas, dates). RAW stores as-is. Defaults to USER_ENTERED.",
                },
            },
            "required": ["spreadsheet_id", "range", "values"],
        },
        method="PUT",
        endpoint="/sheets/{spreadsheet_id}/values/{range}",
        path_params=["spreadsheet_id", "range"],
    ),
    ToolDef(
        name="append_sheet_rows",
        description=(
            "Append rows to a spreadsheet after the last row of existing data. "
            "Use the full column range of the table, e.g. 'Sheet1!A:D' — do not include a row number. "
            "Tab names that contain spaces must be single-quoted: e.g. \"'My Sheet'!A:D\". "
            "values is a 2D array; each inner array is one row, e.g. [['Alice', '100'], ['Bob', '200']]."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "spreadsheet_id": {
                    "type": "string",
                    "description": "The spreadsheet ID.",
                },
                "range": {
                    "type": "string",
                    "description": "A1 notation column range indicating the table, e.g. 'Sheet1!A:D'. Single-quote tab names with spaces: \"'My Sheet'!A:D\".",
                },
                "values": {
                    "type": "array",
                    "items": {
                        "type": "array",
                        "items": {"type": ["string", "number", "boolean", "null"]},
                    },
                    "description": "2D array of rows to append. Each inner array is one row.",
                },
                "value_input_option": {
                    "type": "string",
                    "enum": ["USER_ENTERED", "RAW"],
                    "description": "USER_ENTERED parses values as if typed by a user. Defaults to USER_ENTERED.",
                },
            },
            "required": ["spreadsheet_id", "range", "values"],
        },
        method="POST",
        endpoint="/sheets/{spreadsheet_id}/values/{range}/append",
        path_params=["spreadsheet_id", "range"],
    ),
    ToolDef(
        name="clear_sheet_range",
        description="Clear all values in a spreadsheet range. Formatting is preserved; only values are removed.",
        input_schema={
            "type": "object",
            "properties": {
                "spreadsheet_id": {
                    "type": "string",
                    "description": "The spreadsheet ID.",
                },
                "range": {
                    "type": "string",
                    "description": "A1 notation range to clear, e.g. 'Sheet1!A2:D50'.",
                },
            },
            "required": ["spreadsheet_id", "range"],
        },
        method="DELETE",
        endpoint="/sheets/{spreadsheet_id}/values/{range}",
        path_params=["spreadsheet_id", "range"],
    ),

    # -------------------------------------------------------------------------
    # Finance
    # -------------------------------------------------------------------------
    ToolDef(
        name="get_subscriptions",
        description="List active subscriptions. Pass all=true to include inactive ones.",
        input_schema={
            "type": "object",
            "properties": {
                "all": {
                    "type": "boolean",
                    "description": "Include inactive subscriptions. Defaults to false.",
                },
            },
        },
        method="GET",
        endpoint="/finance/subscriptions",
    ),
    ToolDef(
        name="add_subscription",
        description="Add a new recurring subscription or bill.",
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Service or bill name."},
                "amount": {"type": "number", "description": "Billing amount (use estimate for variable bills)."},
                "frequency": {
                    "type": "string",
                    "enum": ["monthly", "annual", "weekly", "biweekly"],
                    "description": "Billing frequency. Defaults to monthly.",
                },
                "category": {"type": "string", "description": "Category, e.g. 'streaming', 'utilities', 'software'."},
                "type": {
                    "type": "string",
                    "enum": ["subscription", "bill"],
                    "description": "subscription for optional services, bill for financial obligations. Defaults to subscription.",
                },
                "variable_amount": {
                    "type": "boolean",
                    "description": "True if the amount varies month to month (e.g. electricity). Amount is then an estimate.",
                },
                "billing_day": {
                    "type": "integer",
                    "description": "Day of month the charge occurs (1–31). Use for monthly items.",
                },
                "next_billing_date": {
                    "type": "string",
                    "description": "Next due/charge date as YYYY-MM-DD. Use for annual, weekly, or biweekly items.",
                },
                "notes": {"type": "string", "description": "Optional notes."},
            },
            "required": ["name", "amount", "category"],
        },
        method="POST",
        endpoint="/finance/subscriptions",
    ),
    ToolDef(
        name="update_subscription",
        description="Update fields on an existing subscription or bill.",
        input_schema={
            "type": "object",
            "properties": {
                "subscription_id": {"type": "string", "description": "Subscription UUID."},
                "name": {"type": "string"},
                "amount": {"type": "number"},
                "frequency": {
                    "type": "string",
                    "enum": ["monthly", "annual", "weekly", "biweekly"],
                },
                "category": {"type": "string"},
                "type": {"type": "string", "enum": ["subscription", "bill"]},
                "variable_amount": {"type": "boolean"},
                "billing_day": {"type": "integer", "description": "Day of month (1–31)."},
                "next_billing_date": {"type": "string", "description": "Next due date as YYYY-MM-DD."},
                "active": {"type": "boolean", "description": "Set false to deactivate."},
                "notes": {"type": "string"},
            },
            "required": ["subscription_id"],
        },
        method="PATCH",
        endpoint="/finance/subscriptions/{subscription_id}",
        path_params=["subscription_id"],
    ),
    ToolDef(
        name="delete_subscription",
        description="Deactivate (soft-delete) a subscription or bill by ID.",
        input_schema={
            "type": "object",
            "properties": {
                "subscription_id": {"type": "string", "description": "Subscription UUID."},
            },
            "required": ["subscription_id"],
        },
        method="DELETE",
        endpoint="/finance/subscriptions/{subscription_id}",
        path_params=["subscription_id"],
    ),
    ToolDef(
        name="get_budget",
        description="List all budget category limits.",
        input_schema={"type": "object", "properties": {}},
        method="GET",
        endpoint="/finance/budget",
    ),
    ToolDef(
        name="set_budget_limit",
        description="Set or update the monthly spending limit for a budget category.",
        input_schema={
            "type": "object",
            "properties": {
                "category": {"type": "string", "description": "Budget category name."},
                "monthly_limit": {"type": "number", "description": "Monthly limit in dollars."},
            },
            "required": ["category", "monthly_limit"],
        },
        method="PUT",
        endpoint="/finance/budget/{category}",
        path_params=["category"],
    ),
    ToolDef(
        name="delete_budget",
        description="Remove a budget category limit.",
        input_schema={
            "type": "object",
            "properties": {
                "category": {"type": "string", "description": "Budget category to remove."},
            },
            "required": ["category"],
        },
        method="DELETE",
        endpoint="/finance/budget/{category}",
        path_params=["category"],
    ),
    ToolDef(
        name="get_income",
        description="List active income sources.",
        input_schema={"type": "object", "properties": {}},
        method="GET",
        endpoint="/finance/income",
    ),
    ToolDef(
        name="add_income_source",
        description="Add a new income source.",
        input_schema={
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "Income source name, e.g. 'Job', 'Freelance'."},
                "amount": {"type": "number", "description": "Income amount."},
                "frequency": {
                    "type": "string",
                    "enum": ["monthly", "annual", "weekly", "biweekly"],
                    "description": "Pay frequency. Defaults to monthly.",
                },
            },
            "required": ["source", "amount"],
        },
        method="POST",
        endpoint="/finance/income",
    ),
    ToolDef(
        name="delete_income",
        description="Deactivate (soft-delete) an income source by ID.",
        input_schema={
            "type": "object",
            "properties": {
                "income_id": {"type": "string", "description": "Income source UUID."},
            },
            "required": ["income_id"],
        },
        method="DELETE",
        endpoint="/finance/income/{income_id}",
        path_params=["income_id"],
    ),
    ToolDef(
        name="get_upcoming_bills",
        description=(
            "List subscriptions and bills with a known billing date that are due within the next N days "
            "(default 30), sorted by due date. Only returns items that have billing_day or next_billing_date set."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "days": {"type": "integer", "description": "Look-ahead window in days. Defaults to 30."},
            },
        },
        method="GET",
        endpoint="/finance/upcoming",
    ),
    ToolDef(
        name="get_monthly_summary",
        description=(
            "Get a computed monthly financial summary: income, subscription/bill costs, net estimated, "
            "plus full lists of income sources, subscriptions, bills, and budget limits."
        ),
        input_schema={"type": "object", "properties": {}},
        method="GET",
        endpoint="/finance/summary",
    ),

    # -------------------------------------------------------------------------
    # Memory (internal — does not call the gateway)
    # -------------------------------------------------------------------------
    # ── Places ──────────────────────────────────────────────────────────────
    ToolDef(
        name="search_places",
        description=(
            "Search for real-world places using natural language. Use for restaurants, "
            "coffee shops, gyms, stores, attractions, etc. Optionally provide the user's "
            "current coordinates for 'near me' queries."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural language search, e.g. 'quiet coffee shop' or 'Italian restaurants in Broad Ripple'.",
                },
                "latitude": {
                    "type": "number",
                    "description": "User's current latitude for location bias.",
                },
                "longitude": {
                    "type": "number",
                    "description": "User's current longitude for location bias.",
                },
                "radius_meters": {
                    "type": "number",
                    "description": "Search radius in meters when using location bias. Defaults to 5000.",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Max results to return (1-20). Defaults to 5.",
                },
            },
            "required": ["query"],
        },
        method="POST",
        endpoint="/places/search",
    ),
    ToolDef(
        name="get_place_details",
        description="Get full details for a specific place by its Place ID — hours, phone, website, reviews, etc.",
        input_schema={
            "type": "object",
            "properties": {
                "place_id": {
                    "type": "string",
                    "description": "The Place ID from a search_places result.",
                },
            },
            "required": ["place_id"],
        },
        method="GET",
        endpoint="/places/{place_id}",
        path_params=["place_id"],
    ),
    # ── Model escalation ─────────────────────────────────────────────────────
    ToolDef(
        name="request_escalation",
        description=(
            "Call this when the task requires complex reasoning, nuanced writing, or multi-step "
            "synthesis that exceeds your current confidence. Switches to a more capable model "
            "for all subsequent turns in this conversation turn."
        ),
        input_schema={"type": "object", "properties": {}},
        method="INTERNAL",
        endpoint="",
    ),
    # ── Tool expansion ───────────────────────────────────────────────────────
    ToolDef(
        name="request_tools",
        description=(
            "Expand your available tools when you need a capability not currently shown. "
            "Call this before telling the user you lack a capability. "
            "Valid categories: calendar, tasks, email, notify, kb, web, drive, github, sheets, finance, places."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "categories": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Tool categories to add, e.g. ['github'] or ['drive', 'sheets'].",
                },
            },
            "required": ["categories"],
        },
        method="INTERNAL",
        endpoint="",
    ),
    # ── Memory ──────────────────────────────────────────────────────────────
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


# ---------------------------------------------------------------------------
# Selective tool injection
# ---------------------------------------------------------------------------

TOOL_CATEGORIES: dict[str, list[str]] = {
    "calendar": ["get_events", "check_availability", "create_event", "update_event",
                 "delete_event", "search_events"],
    "tasks":    ["get_task_lists", "get_tasks", "create_task_list", "rename_task_list",
                 "create_task", "update_task", "delete_task"],
    "email":    ["list_emails", "search_emails", "get_email", "draft_email"],
    "notify":   ["send_notification"],
    "kb":       ["search_knowledge_base", "get_kb_index", "list_kb_sources", "delete_kb_source", "sync_kb"],
    "web":      ["web_search", "fetch_url", "aggregate_search"],
    "drive":    ["list_files", "list_folders", "create_folder", "get_file_info", "read_file",
                 "create_file", "update_file", "append_to_file", "delete_file", "move_file",
                 "copy_file", "copy_file_from_github"],
    "github":   ["list_repos", "get_repo", "list_issues", "get_issue", "create_issue",
                 "update_issue", "add_issue_comment", "list_prs", "get_pr", "add_pr_comment",
                 "create_pr", "search_issues", "get_github_file", "search_code",
                 "list_commits", "get_commit", "list_branches", "list_tags",
                 "list_releases", "get_latest_release", "get_pr_reviews", "get_pr_files",
                 "list_contributors", "compare_refs"],
    "sheets":   ["create_spreadsheet", "get_spreadsheet_info", "read_sheet", "write_sheet",
                 "append_sheet_rows", "clear_sheet_range"],
    "finance":  ["get_subscriptions", "add_subscription", "update_subscription", "delete_subscription",
                 "get_budget", "set_budget_limit", "delete_budget",
                 "get_income", "add_income_source", "delete_income",
                 "get_upcoming_bills", "get_monthly_summary"],
    "places":   ["search_places", "get_place_details"],
}

# ---------------------------------------------------------------------------
# Think mode — fixed tool set for autonomous proactive reasoning
# Read broadly, write narrowly.
# ---------------------------------------------------------------------------

THINK_TOOLS: frozenset[str] = frozenset({
    # Read — calendar
    "get_events", "check_availability", "search_events",
    # Read — tasks
    "get_task_lists", "get_tasks",
    # Read — email
    "list_emails", "search_emails", "get_email",
    # Read — web/search
    "web_search", "aggregate_search",
    # Read — KB
    "search_knowledge_base", "list_kb_sources",
    # Read — Drive
    "list_files", "list_folders", "get_file_info", "read_file",
    # Read — GitHub
    "list_repos", "get_repo", "list_issues", "get_issue",
    "list_prs", "get_pr", "search_issues", "get_github_file",
    "list_commits", "get_commit", "list_branches",
    "list_releases", "get_latest_release", "get_pr_reviews",
    "get_pr_files", "list_contributors",
    # Read — Finance
    "get_subscriptions", "get_budget", "get_income",
    "get_upcoming_bills", "get_monthly_summary",
    # Read — Sheets
    "get_spreadsheet_info", "read_sheet",
    # Write (limited)
    "send_notification", "create_task", "memory_update",
    "append_to_file", "create_file", "sync_kb",
})


def get_think_tool_schemas() -> list[dict]:
    """Return the fixed tool schema list for think mode."""
    schemas = [
        {"name": t.name, "description": t.description, "input_schema": t.input_schema}
        for t in TOOLS
        if t.name in THINK_TOOLS
    ]
    if schemas:
        schemas[-1]["cache_control"] = {"type": "ephemeral"}
    return schemas


_CATEGORY_PATTERNS: dict[str, re.Pattern] = {
    "calendar": re.compile(
        r'\b(calendar|event|meeting|appointment|schedule|busy|free|availability|rsvp|invite'
        r'|standup|stand-up|reschedule|tomorrow|tonight|zoom)\b', re.I),
    "tasks":    re.compile(
        r'\b(tasks?|todo|to-do|to do|to.do list|reminder|checklist|things to do|get done'
        r'|mark.*done)\b', re.I),
    "email":    re.compile(
        r'\b(email|gmail|inbox|mail|unread|draft|subject|reply|forward|messages?)\b', re.I),
    "notify":   re.compile(
        r'\b(notify|notification|push.?notification|alert|pushover'
        r'|ping me|heads.?up|let me know when|remind me)\b', re.I),
    "kb":       re.compile(
        r'\b(knowledge.?base|my notes?|my docs?|recall'
        r'|what (do |have )?i (know|noted|written|saved|documented|decided|said|mentioned)'
        r'|look up in my notes?|search my notes?|in my notes?'
        r'|do i have (any |a )?(info|notes?|docs?|document|file|something) (on|about|for|regarding)'
        r'|from my notes?|check my notes?'
        r'|what did (i|we) (discuss|decide|talk about|say|mention|write)'
        r'|do you (know|have|remember) (anything|something) about'
        r'|find (my |the )?(notes?|docs?|info|document) (on|about|for)'
        r'|what.s in my (notes?|docs?|kb|knowledge)'
        r'|is there (anything|something) (on|about|regarding)'
        r'|remind me (what|about|of)'
        r'|what (is|are) (my|the) (notes?|docs?|info) (on|about))\b', re.I),
    "web":      re.compile(
        r'\b(look up|google|browse|find out|who is|news'
        r'|current (price|weather|news|status|version|rate|score)'
        r'|latest (news|version|release|update|price|score)'
        r'|search (the )?web|search online|search for'
        r'|reddit|hacker news|bluesky|social media'
        r'|what.s (trending|happening|going on)'
        r'|aggregate search|cross.?platform)\b', re.I),
    "drive":    re.compile(
        r'\b(file|drive|document|folder|google drive|gdrive|upload|download)\b', re.I),
    "github":   re.compile(
        r'\b(github|repo|repository|issue|pull request|\bpr\b|commit|branch|merge|fork|git)\b',
        re.I),
    "sheets":   re.compile(
        r'\b(sheet|spreadsheet|excel|google sheets|csv|row|column|cell|table)\b', re.I),
    "finance":  re.compile(
        r'\b(subscri(ption|be)|budget|income|spend(ing)?|expense|bill(ing|s?)|payment|monthly cost'
        r'|how much (do i |am i )?pay|afford|net (income|pay)|salary|paycheck'
        r'|financial|finance|money|cash flow|subscription|netflix|spotify|hulu'
        r'|due (date|on|this)|when (is|does|do).*charge|upcoming (bill|payment|charge))\b', re.I),
    "places":   re.compile(
        r'\b(restaurant|cafe|coffee|bar|gym|store|shop|nearby|near me|places?|food|eat|drink'
        r'|hotel|directions?|open.*(now|today)|hours|visit|dine|dining|takeout|delivery'
        r'|find (a |the |me )?(place|spot|restaurant|cafe|bar)|where (can|should) (i|we))\b', re.I),
}

_ALWAYS_INCLUDED: frozenset[str] = frozenset({"memory_update", "request_tools", "request_escalation"})
_DEFAULT_CATEGORIES: frozenset[str] = frozenset({"calendar", "tasks", "kb", "web"})
_STICKY_CATEGORIES: frozenset[str] = frozenset({"kb"})  # always injected regardless of pattern match
_CO_SELECT: dict[str, frozenset[str]] = {
    "sheets": frozenset({"drive"}),
}


def select_tools(user_message: str) -> list[dict]:
    """Return tool schemas for categories matching the user message.

    Falls back to _DEFAULT_CATEGORIES if no patterns match.
    Co-selection rules in _CO_SELECT are applied after initial matching.
    memory_update is always included regardless.
    Preserves original TOOLS ordering. cache_control applied to last schema.
    """
    matched = {cat for cat, pat in _CATEGORY_PATTERNS.items() if pat.search(user_message)}
    categories = matched if matched else _DEFAULT_CATEGORIES
    categories = categories | _STICKY_CATEGORIES

    # Apply co-selection: some categories implicitly require others
    for cat in list(categories):
        categories = categories | _CO_SELECT.get(cat, frozenset())

    selected_names: set[str] = set(_ALWAYS_INCLUDED)
    for cat in categories:
        selected_names.update(TOOL_CATEGORIES.get(cat, []))

    schemas = [
        {"name": t.name, "description": t.description, "input_schema": t.input_schema}
        for t in TOOLS
        if t.name in selected_names
    ]
    if schemas:
        schemas[-1]["cache_control"] = {"type": "ephemeral"}
    return schemas


def expand_tools(current_tools: list[dict], categories: list[str]) -> tuple[list[dict], str]:
    """Merge tool schemas for the requested categories into the current tool list.

    Returns (updated_tools, result_message). Safe to call with already-loaded categories.
    """
    existing_names = {t["name"] for t in current_tools}
    new_defs = []
    for cat in categories:
        for name in TOOL_CATEGORIES.get(cat, []):
            if name not in existing_names:
                td = _tool_index.get(name)
                if td:
                    new_defs.append({
                        "name": td.name,
                        "description": td.description,
                        "input_schema": td.input_schema,
                    })
                    existing_names.add(name)

    if not new_defs:
        return current_tools, f"No new tools added — {categories} already loaded or unknown."

    expanded = list(current_tools)
    if expanded and "cache_control" in expanded[-1]:
        expanded[-1] = {k: v for k, v in expanded[-1].items() if k != "cache_control"}
    expanded.extend(new_defs)
    expanded[-1]["cache_control"] = {"type": "ephemeral"}

    added_names = [t["name"] for t in new_defs]
    msg = f"Tools expanded. Added {len(added_names)} tools from {categories}: {', '.join(added_names)}."
    return expanded, msg


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


async def execute_tool(name: str, args: dict[str, Any]) -> ToolResult:
    """Dispatch a tool call and return a ToolResult for the LLM and audit log."""
    t0 = time.perf_counter()

    def _ms() -> int:
        return int((time.perf_counter() - t0) * 1000)

    def _err(msg: str) -> ToolResult:
        return ToolResult(content=msg, status="error", error=msg, duration_ms=_ms())

    def _ok(content: str) -> ToolResult:
        return ToolResult(content=content, status="success", error=None, duration_ms=_ms())

    tool = _tool_index.get(name)
    if tool is None:
        return _err(f"Unknown tool: {name}")

    if tool.method == "INTERNAL":
        return await _execute_internal(name, args, t0)

    # Cache lookup — read-only tools only
    cache_key = None
    if name in _CACHEABLE_TOOLS:
        cache_key = (name, tuple(sorted((k, str(v)) for k, v in args.items())))
        cached = _TOOL_CACHE.get(cache_key)
        if cached is not None:
            return ToolResult(
                content=cached.content, status=cached.status,
                error=cached.error, duration_ms=0,
            )

    # SSRF guard — validate any URL argument before forwarding to the gateway
    if "url" in args:
        ssrf_err = _check_ssrf(str(args["url"]))
        if ssrf_err:
            return _err(ssrf_err)

    # Interpolate path params, keeping remaining args for query/body
    endpoint = tool.endpoint
    remaining = dict(args)
    for param in tool.path_params:
        val = remaining.pop(param, None)
        if val is None:
            return _err(f"Missing required path parameter: {param}")
        endpoint = endpoint.replace(f"{{{param}}}", quote(str(val), safe=""))

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
                    return _ok("Deleted successfully.")
            else:
                return _err(f"Unsupported method: {tool.method}")
    except httpx.TimeoutException:
        return _err("Request timed out.")
    except httpx.RequestError as e:
        return _err(f"Request error: {e}")

    if not resp.is_success:
        return _err(f"Error {resp.status_code}: {resp.text}")

    try:
        result = _ok(json.dumps(resp.json()))
    except Exception:
        result = _ok(resp.text)

    if cache_key:
        _TOOL_CACHE[cache_key] = result
    return result


async def _execute_internal(name: str, args: dict[str, Any], t0: float) -> ToolResult:
    """Handle internal tools that don't call the gateway."""
    def _ms() -> int:
        return int((time.perf_counter() - t0) * 1000)

    if name == "memory_update":
        try:
            fact = await upsert_fact(
                fact_type=args["fact_type"],
                key=args["key"],
                value=args["value"],
                confidence=1.0,
                source="user_explicit",
            )
            content = f"Remembered: [{fact['fact_type']}] {fact['key']} = {fact['value']}"
            return ToolResult(content=content, status="success", error=None, duration_ms=_ms())
        except Exception as e:
            msg = f"Memory update failed: {e}"
            return ToolResult(content=msg, status="error", error=msg, duration_ms=_ms())

    if name == "request_tools":
        # Normally handled inline by the agent loop; this is a safety fallback.
        return ToolResult(content="Tool expansion handled by agent loop.", status="success", error=None, duration_ms=_ms())

    if name == "request_escalation":
        # Normally handled inline by the agent loop; this is a safety fallback.
        return ToolResult(content="Escalation handled by agent loop.", status="success", error=None, duration_ms=_ms())

    msg = f"Unknown internal tool: {name}"
    return ToolResult(content=msg, status="error", error=msg, duration_ms=_ms())
