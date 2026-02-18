# Short-Term Expansion Plan

Covers tool catalog cleanup + three new capability areas. Everything touches
only the gateway and tools.json — no architectural changes.

---

## Phase 0 — Tool Catalog Cleanup

Clean up existing tools before adding new ones. Gateway endpoints that back
deprecated tools can be removed or left in place (they're harmless); the goal
is removing agent confusion.

### Remove

| Tool | Reason |
|------|--------|
| `get_today` | Redundant with `get_events`. Agent can pass `days=1` and filter. |
| `get_upcoming_tasks` | Hardcodes list names, enforces arbitrary day window. Replace with `get_tasks` (below). |

### Modify

**`get_recent_emails` + `get_unread_emails` → `list_emails`**

Consolidate into one tool. Model was forced to choose between two nearly
identical tools with overlapping use cases.

```json
{
  "name": "list_emails",
  "description": "List emails from the primary inbox. Filter by unread status and/or recency.",
  "category": "email",
  "method": "GET",
  "endpoint": "/email",
  "parameters": [
    {
      "name": "unread_only",
      "type": "boolean",
      "description": "If true, return only unread emails. Defaults to false.",
      "required": false
    },
    {
      "name": "hours",
      "type": "integer",
      "description": "Limit to emails received within the last N hours (1–168). Omit for no time filter.",
      "required": false
    },
    {
      "name": "max_results",
      "type": "integer",
      "description": "Max emails to return (1–50). Defaults to 20.",
      "required": false
    }
  ]
}
```

Gateway: merge `/email/recent` and `/email/unread` into `GET /email` with
query params, or keep both backing routes and add a new unified one.

**`update_task` — fix `status` field**

```json
{
  "name": "status",
  "type": "string",
  "description": "Task completion status. Use 'completed' to mark done, 'needsAction' to reopen.",
  "required": false
}
```

**`draft_email` — clarify it does not send**

```json
{
  "description": "Save an email as a draft in Gmail. Does not send — user must send from Gmail."
}
```

**`get_task_lists` — remove procedural instruction from description**

```json
{
  "description": "Get all task lists with their IDs and names."
}
```

### Add (tasks read side)

`get_upcoming_tasks` is gone but there's no way to read tasks in a specific
list. Add `get_tasks`:

```json
{
  "name": "get_tasks",
  "description": "Get tasks from a specific task list. Returns all non-completed tasks by default.",
  "category": "tasks",
  "method": "GET",
  "endpoint": "/tasks/lists/{list_id}/tasks",
  "parameters": [
    {
      "name": "list_id",
      "type": "string",
      "description": "The task list ID. Get available lists with get_task_lists.",
      "required": true
    },
    {
      "name": "include_completed",
      "type": "boolean",
      "description": "Include completed tasks. Defaults to false.",
      "required": false
    }
  ]
}
```

**Gateway:** `GET /tasks/lists/{list_id}/tasks` — straightforward passthrough
to Google Tasks API `tasks.list`. Strips completed by default.

---

## Phase 1 — Drive / Storage

Agent goes from read-only on KB chunks to full file lifecycle on Drive.

### Gateway Endpoints

```
GET    /storage/files                   — list files (with optional folder/query filter)
GET    /storage/files/{id}/content      — fetch full file content as text
POST   /storage/files                   — create file
PUT    /storage/files/{id}              — overwrite file content
DELETE /storage/files/{id}              — trash file
```

`list_files` needs a way to scope results — Drive is large. Support `folder_id`
and a `query` string (passed to Drive's `q` param). The agent can call
`list_files` to find a file ID, then `get_file` to read it.

Skip `/storage/files/{id}/copy` and `/storage/folders` for now — low frequency,
add later.

### Tool Schemas

```json
{
  "name": "list_files",
  "description": "List files in Google Drive. Filter by folder or search query. Use to find a file ID before reading or modifying it.",
  "category": "storage",
  "method": "GET",
  "endpoint": "/storage/files",
  "parameters": [
    {
      "name": "folder_id",
      "type": "string",
      "description": "Limit results to a specific Drive folder ID.",
      "required": false
    },
    {
      "name": "query",
      "type": "string",
      "description": "Drive search query string, e.g. 'name contains \"resume\"'.",
      "required": false
    },
    {
      "name": "max_results",
      "type": "integer",
      "description": "Max files to return (1–50). Defaults to 20.",
      "required": false
    }
  ]
}
```

```json
{
  "name": "get_file",
  "description": "Fetch the full text content of a Google Drive file by ID.",
  "category": "storage",
  "method": "GET",
  "endpoint": "/storage/files/{id}/content",
  "parameters": [
    {
      "name": "file_id",
      "type": "string",
      "description": "The Drive file ID.",
      "required": true
    }
  ]
}
```

```json
{
  "name": "create_file",
  "description": "Create a new file in Google Drive with text content.",
  "category": "storage",
  "method": "POST",
  "endpoint": "/storage/files",
  "parameters": [
    {
      "name": "name",
      "type": "string",
      "description": "File name including extension, e.g. 'meeting-notes.md'.",
      "required": true
    },
    {
      "name": "content",
      "type": "string",
      "description": "File content as plain text.",
      "required": true
    },
    {
      "name": "folder_id",
      "type": "string",
      "description": "Parent folder ID. Defaults to root if omitted.",
      "required": false
    },
    {
      "name": "mime_type",
      "type": "string",
      "description": "MIME type, e.g. 'text/plain', 'text/markdown'. Defaults to text/plain.",
      "required": false
    }
  ]
}
```

```json
{
  "name": "update_file",
  "description": "Overwrite the content of an existing Google Drive file.",
  "category": "storage",
  "method": "PUT",
  "endpoint": "/storage/files/{id}",
  "parameters": [
    {
      "name": "file_id",
      "type": "string",
      "description": "The Drive file ID.",
      "required": true
    },
    {
      "name": "content",
      "type": "string",
      "description": "New file content. Replaces existing content entirely.",
      "required": true
    }
  ]
}
```

```json
{
  "name": "delete_file",
  "description": "Move a Google Drive file to trash.",
  "category": "storage",
  "method": "DELETE",
  "endpoint": "/storage/files/{id}",
  "parameters": [
    {
      "name": "file_id",
      "type": "string",
      "description": "The Drive file ID.",
      "required": true
    }
  ]
}
```

### Implementation Notes

- `get_file` content export: use Drive's `files.export` for Google Docs/Sheets
  (exports as plain text), `files.get?alt=media` for binary/uploaded files.
  Gateway handles the branch — agent always gets text back.
- `create_file`: use `files.create` with multipart upload. For `.md` files,
  upload as `text/plain` — Drive will store it natively without converting to
  a Google Doc.
- `update_file`: `files.update` with media upload to replace content.
- `delete_file`: `files.update` with `trashed: true` — safer than permanent delete.

---

## Phase 2 — Task List Management

Complete the task story. Currently have task CRUD but can't create or delete
lists, and can't read tasks by list (only the deprecated upcoming endpoint).

### Gateway Endpoints

```
GET    /tasks/lists/{list_id}/tasks         — list tasks in a list  (Phase 0)
POST   /tasks/lists                         — create task list
PATCH  /tasks/lists/{list_id}               — rename task list
DELETE /tasks/lists/{list_id}               — delete task list
```

Skipping `complete/uncomplete` shortcuts and `move` — `update_task` with
`status` already handles complete/uncomplete, and move can wait.

### Tool Schemas

`get_tasks` defined in Phase 0 above.

```json
{
  "name": "create_task_list",
  "description": "Create a new Google Tasks task list.",
  "category": "tasks",
  "method": "POST",
  "endpoint": "/tasks/lists",
  "parameters": [
    {
      "name": "title",
      "type": "string",
      "description": "Task list name.",
      "required": true
    }
  ]
}
```

```json
{
  "name": "rename_task_list",
  "description": "Rename an existing task list.",
  "category": "tasks",
  "method": "PATCH",
  "endpoint": "/tasks/lists/{list_id}",
  "parameters": [
    {
      "name": "list_id",
      "type": "string",
      "description": "The task list ID.",
      "required": true
    },
    {
      "name": "title",
      "type": "string",
      "description": "New task list name.",
      "required": true
    }
  ]
}
```

```json
{
  "name": "delete_task_list",
  "description": "Permanently delete a task list and all its tasks.",
  "category": "tasks",
  "method": "DELETE",
  "endpoint": "/tasks/lists/{list_id}",
  "parameters": [
    {
      "name": "list_id",
      "type": "string",
      "description": "The task list ID.",
      "required": true
    }
  ]
}
```

---

## Phase 3 — Web Search (Tavily)

Breaks the closed-world limitation. KB first, web second — agent decides when
KB results are insufficient.

### Gateway Endpoints

```
POST   /search/web          — Tavily search, returns snippets + URLs + extracted content
POST   /search/web/fetch    — fetch and extract clean text from a specific URL
```

Gateway wraps Tavily so the agent never touches the API key, and the endpoint
interface stays stable if the provider changes.

### Tool Schemas

```json
{
  "name": "web_search",
  "description": "Search the web for current information. Use when the knowledge base doesn't have the answer or the topic requires up-to-date data.",
  "category": "search",
  "method": "POST",
  "endpoint": "/search/web",
  "parameters": [
    {
      "name": "query",
      "type": "string",
      "description": "Search query.",
      "required": true
    },
    {
      "name": "max_results",
      "type": "integer",
      "description": "Number of results to return (1–10). Defaults to 5.",
      "required": false
    },
    {
      "name": "search_depth",
      "type": "string",
      "description": "Tavily search depth. 'basic' is faster, 'advanced' does deeper extraction. Defaults to 'basic'.",
      "required": false
    }
  ]
}
```

```json
{
  "name": "fetch_url",
  "description": "Fetch and extract the readable text content from a specific URL. Use when you have a URL and need its full content.",
  "category": "search",
  "method": "POST",
  "endpoint": "/search/web/fetch",
  "parameters": [
    {
      "name": "url",
      "type": "string",
      "description": "The URL to fetch.",
      "required": true
    }
  ]
}
```

### Implementation Notes

- Tavily's `/search` endpoint returns `results[].content` which is
  pre-extracted text — no HTML parsing needed on the gateway side.
- `search_depth=advanced` uses more Tavily credits but does better extraction
  on complex pages. Expose it as a param and let the agent choose.
- `fetch_url`: use Tavily's `/extract` endpoint, or fall back to
  `httpx` + `trafilatura` if Tavily's extract hits rate limits.
- Store `TAVILY_API_KEY` in gateway secrets alongside existing keys.

---

## Checklist

### Phase 0 — Cleanup
- [ ] Remove `GET /calendar/today` endpoint (or leave, just remove from tools.json)
- [ ] Remove `GET /tasks/upcoming` endpoint
- [ ] Add `GET /email` unified endpoint (or alias existing routes)
- [ ] Update `tools.json`: remove `get_today`, `get_upcoming_tasks`, `get_recent_emails`, `get_unread_emails`
- [ ] Add `list_emails` to tools.json
- [ ] Add `get_tasks` to tools.json
- [ ] Fix `update_task.status` description
- [ ] Fix `draft_email` description
- [ ] Fix `get_task_lists` description

### Phase 1 — Drive / Storage
- [ ] `GET /storage/files` — list with folder/query filter
- [ ] `GET /storage/files/{id}/content` — export as text
- [ ] `POST /storage/files` — create with content
- [ ] `PUT /storage/files/{id}` — overwrite content
- [ ] `DELETE /storage/files/{id}` — trash
- [ ] Add all 5 tools to tools.json

### Phase 2 — Task List Management
- [ ] `POST /tasks/lists`
- [ ] `PATCH /tasks/lists/{list_id}`
- [ ] `DELETE /tasks/lists/{list_id}`
- [ ] Add `create_task_list`, `rename_task_list`, `delete_task_list` to tools.json

### Phase 3 — Web Search
- [ ] Add `TAVILY_API_KEY` to gateway secrets
- [ ] `POST /search/web` — Tavily search wrapper
- [ ] `POST /search/web/fetch` — URL extraction
- [ ] Add `web_search`, `fetch_url` to tools.json