# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**Sazed** is the personal AI agent: it runs an agent loop (LLM + tool calls), talks to the api-gateway for calendar, tasks, email, storage, notifications, knowledge-base, web search, GitHub, and Google Sheets, and keeps structured memory and conversation sessions. Part of a larger personal AI ecosystem — see `api-gateway` and `knowledge-base` repos.

**Stack:** FastAPI (Python 3.11+), Anthropic SDK (tool_use + streaming), asyncpg for session/memory, Poetry. Deployed via Docker to GCP Cloud Run (workflow present but currently disabled).

## Commands

```bash
poetry install
poetry run uvicorn app.main:app --reload   # dev server
ruff check app/
ruff format app/
```

Chat CLI: `python chat.py` (separate terminal, hits local agent).

No test suite yet.

## Architecture

**Entry point:** `app/main.py` — FastAPI app with lifespan (DB pool init/close), CORS, API key auth when `API_KEY` is set. Health and tools are public; all other routes require `X-API-Key` when `API_KEY` is set.

**Configuration:** `app/config.py` — pydantic-settings from `.env`. Key vars: `GATEWAY_URL`, `GATEWAY_API_KEY`, `ANTHROPIC_API_KEY`, `DATABASE_URL`, `haiku_model`, `sonnet_model`, `conversations_folder_id`, `session_summarization`, `session_window_size`.

**Flow:** `POST /chat` → agent loop in `app/agent/loop.py`: load memory and session history, apply context window compression if needed, call LLM (Haiku or Sonnet, escalating at turn 3), on `tool_use` execute tools via `app/agent/tools.py` (all tools call the gateway except `memory_update`), repeat up to 5 turns, then return final response and persist messages. Streaming via `GET /chat/stream` (SSE). Session processing in `app/agent/session.py` runs fact extraction, summarization, and optional KB ingestion after a conversation.

**Tools (`app/agent/tools.py`):** Registry of tool definitions (name, description, input_schema, method, endpoint). All gateway-backed tools use a single base URL and API key. Only `memory_update` is INTERNAL (writes to agent memory, no gateway call). Executor builds URL from `settings.gateway_url + endpoint`, sends request with `X-API-Key`, returns response body or error string. SSRF guard blocks private/loopback IPs on URL arguments.

**Tool categories and gateway paths:**
- Calendar: `get_events`, `check_availability`, `create_event`, `update_event`, `delete_event`, `search_events` → `/calendar/*`
- Tasks: `get_task_lists`, `get_tasks`, `create_task_list`, `rename_task_list`, `create_task`, `update_task`, `delete_task` → `/tasks/*`
- Email: `list_emails`, `search_emails`, `get_email`, `draft_email` → `/email/*`
- Notifications: `send_notification` → `/notify`
- Knowledge Base: `search_knowledge_base`, `list_kb_sources`, `delete_kb_source`, `sync_kb` → `/kb/*`
- Web Search: `web_search`, `fetch_url` → `/search/web*`
- Storage (Drive): `list_files`, `list_folders`, `create_folder`, `get_file_info`, `read_file`, `create_file`, `update_file`, `append_to_file`, `delete_file`, `move_file`, `copy_file`, `copy_file_from_github` → `/storage/*`
- GitHub: `list_repos`, `get_repo`, `list_issues`, `get_issue`, `create_issue`, `update_issue`, `add_issue_comment`, `list_prs`, `get_pr`, `add_pr_comment`, `create_pr`, `search_issues`, `get_github_file`, `search_code` → `/github/*`
- Google Sheets: `create_spreadsheet`, `get_spreadsheet_info`, `read_sheet`, `write_sheet`, `append_sheet_rows`, `clear_sheet_range` → `/sheets/*`
- Memory (internal): `memory_update`

**Routers:** `health.py` (GET /health), `chat.py` (POST /chat, GET /chat/stream), `conversations.py` (GET /conversations, GET /conversations/{id}, POST /conversations/{id}/process), `memory.py` (GET /memory, PUT /memory, DELETE /memory/{id}), `kb.py` (GET /kb/stats, /kb/sources, /kb/files, POST /kb/search, /kb/sync, DELETE /kb/files/{id}, DELETE /kb), `tools.py` (GET /tools).

**Database:** `app/db.py` — asyncpg pool for sessions and agent_memory. Sessions store `context_summary` and `summarized_through` for context window management.

**Context window:** When a session exceeds `session_window_size` (default 15) messages, overflow is compressed into a rolling summary via Haiku and stored in the session row. The agent sees `[summary_pair] + recent_messages`.

**Session processing (`session.py`):** After each turn, `process_session` runs in parallel: fact extraction (Haiku → upsert to `agent_memory`), optional summary, optional structured KB entry written to Drive then synced. Controlled by `conversations_folder_id` and `session_summarization` settings.

## Module Layout

```
app/
  main.py              — FastAPI app, lifespan, router mounts
  config.py            — Settings (pydantic-settings)
  dependencies.py      — verify_api_key
  db.py                — asyncpg pool init/close/get
  agent/
    loop.py            — Agent loop: memory + session → LLM → tools → response; run_turn + run_turn_stream
    tools.py           — TOOLS list, get_tool_schemas(), execute_tool(), _execute_internal()
    memory.py          — agent_memory store, prompt formatting, upsert_fact
    session.py         — load/save messages, context compression, post-session fact extraction + summary + KB ingestion
  routers/
    health.py          — GET /health
    chat.py            — POST /chat, GET /chat/stream (SSE)
    conversations.py   — GET /conversations, GET /conversations/{id}, POST /conversations/{id}/process
    memory.py          — GET /memory, PUT /memory, DELETE /memory/{id}
    kb.py              — KB proxy: stats, sources, search, sync, delete
    tools.py           — GET /tools (agent tool registry)
chat.py                — CLI for testing (calls local /chat)
```

## Key Conventions

- **Ruff** for linting/formatting: line length 100, Python 3.11.
- All gateway calls via httpx in `tools.py`; single timeout (30s) per request.
- Config: `from app.config import settings`.
- Session and memory persistence: asyncpg when `DATABASE_URL` is set.
- Streaming uses SSE via `StreamingResponse` in `routers/chat.py`; events: `session`, `tool_start`, `tool_done`, `text_delta`, `done`.
- Model selection: Haiku for turns 0–2, Sonnet from turn 3 onward (`_select_model` in `loop.py`).
- Prompt caching: system blocks and the last tool schema use `cache_control: ephemeral`.
