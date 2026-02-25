# Sazed

Personal AI agent. Orchestrates tools via the api-gateway to answer questions, manage calendar/tasks/email/drive/github, and maintain persistent memory across conversations.

## Ecosystem

Part of a personal AI ecosystem:

| Repo | Role |
|------|------|
| **sazed** *(this repo)* | Agent loop, tools, memory, sessions |
| [api-gateway](https://github.com/tharpep/api-gateway) | Unified proxy for Google APIs, AI providers, and services |
| [knowledge-base](https://github.com/tharpep/knowledge-base) | RAG service — Drive ingestion, pgvector, hybrid retrieval |
| [sazed-frontend](https://github.com/tharpep/sazed-frontend) | React + Tauri desktop/web chat UI |
| [sazed-mcp](https://github.com/tharpep/sazed-mcp) | MCP bridge — exposes Sazed as a tool in Claude Desktop |
| [automations](https://github.com/tharpep/automations) | Scheduled/triggered scripts that call the gateway |

---

## Stack

- **FastAPI** (Python 3.11+) — REST API
- **Anthropic SDK** — agent loop with tool_use (Haiku turns 0–2, Sonnet from turn 3)
- **asyncpg** — Postgres session/memory storage (GCP Cloud SQL)
- **Poetry** — dependency management
- **Docker** → GCP Cloud Run

---

## Setup

```bash
poetry install
cp .env.example .env
# fill in .env (see below)
```

**.env required values:**

```
GATEWAY_URL=https://your-gateway.run.app   # no trailing slash
GATEWAY_API_KEY=your-gateway-api-key
ANTHROPIC_API_KEY=sk-ant-...
DATABASE_URL=postgresql+asyncpg://...
```

Leave `API_KEY` empty for local dev (auth is disabled when unset).

---

## Running

```bash
# Dev server (hot reload)
poetry run uvicorn app.main:app --reload

# Chat CLI (in a separate terminal)
python chat.py
```

API docs at `http://localhost:8000/docs`.

---

## Architecture

```
POST /chat          — blocking, returns full JSON response
POST /chat/stream   — SSE stream, tokens arrive in real time
    │
    ▼
Agent loop (loop.py)
    ├── Loads memory → injects into system prompt
    ├── Loads session history (with context window compression at 15+ msgs)
    ├── LLM call — Haiku (turns 0–2), Sonnet (turn 3+)
    │     stop_reason: tool_use → execute tools → loop back (max 5 turns)
    │     stop_reason: end_turn → return response
    └── Saves messages; runs post-session fact extraction + summarization

Tools (tools.py) → all hit api-gateway with one base URL + one API key
Memory (memory.py) → structured facts injected into every system prompt
```

---

## Project Structure

```
app/
├── main.py              # FastAPI app, router mounts
├── config.py            # Settings (pydantic-settings)
├── dependencies.py      # API key auth
├── db.py                # asyncpg pool
├── agent/
│   ├── loop.py          # Core agent loop (blocking + streaming)
│   ├── tools.py         # Tool registry + executor
│   ├── memory.py        # agent_memory store + prompt formatting
│   └── session.py       # Message persistence, context compression, post-session processing
└── routers/
    ├── health.py        # GET /health
    ├── chat.py          # POST /chat, GET /chat/stream
    ├── conversations.py # GET /conversations, GET /conversations/{id}
    ├── memory.py        # GET /memory, PUT /memory, DELETE /memory/{id}
    ├── kb.py            # KB proxy (search, sync, sources, files, stats)
    └── tools.py         # GET /tools

chat.py                  # Local CLI for testing
```

---

## Tools

All tools call the api-gateway. One base URL, one API key. Full registry with schemas at `GET /tools`.

| Category | Tools |
|----------|-------|
| calendar | get_events, check_availability, create_event, update_event, delete_event, search_events |
| tasks | get_task_lists, get_tasks, create_task_list, rename_task_list, create_task, update_task, delete_task |
| email | list_emails, search_emails, get_email, draft_email |
| notify | send_notification |
| kb | search_knowledge_base, list_kb_sources, delete_kb_source, sync_kb |
| web | web_search, fetch_url |
| storage | list_files, list_folders, create_folder, get_file_info, read_file, create_file, update_file, append_to_file, delete_file, move_file, copy_file, copy_file_from_github |
| github | list_repos, get_repo, list_issues, get_issue, create_issue, update_issue, add_issue_comment, list_prs, get_pr, add_pr_comment, create_pr, search_issues, get_github_file, search_code |
| sheets | create_spreadsheet, get_spreadsheet_info, read_sheet, write_sheet, append_sheet_rows, clear_sheet_range |
| memory | memory_update *(internal — writes to agent_memory, no gateway call)* |

---

## Commands

```bash
poetry install                              # install dependencies
poetry run uvicorn app.main:app --reload    # dev server
ruff check app/                             # lint
ruff format app/                            # format
```
