# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**Sazed** is the personal AI agent: it runs an agent loop (LLM + tool calls), talks to the api-gateway for calendar, tasks, email, storage, notifications, knowledge-base, web search, GitHub, and Google Sheets, and keeps structured memory and conversation sessions. Part of a larger personal AI ecosystem — see `api-gateway` and `knowledge-base` repos.

**Stack:** FastAPI (Python 3.11+), Anthropic SDK (tool_use + streaming), asyncpg for session/memory, Poetry. Deployed via Docker to GCP Cloud Run on every push to main (`.github/workflows/deploy.yml`).

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

**Configuration:** `app/config.py` — pydantic-settings from `.env`. Key vars: `GATEWAY_URL`, `GATEWAY_API_KEY`, `ANTHROPIC_API_KEY`, `DATABASE_URL`, `haiku_model`, `sonnet_model`, `session_summarization`, `session_kb_ingest_enabled`, `session_window_size`.

**Flow:** `POST /chat` → agent loop in `app/agent/loop.py`: load memory and session history, apply context window compression if needed, call LLM (Haiku or Sonnet, escalating at turn 3), on `tool_use` execute tools via `app/agent/tools.py` (all tools call the gateway except `memory_update`), repeat up to 5 turns, then return final response and persist messages. Streaming via `GET /chat/stream` (SSE). Session processing in `app/agent/session.py` runs fact extraction, summarization, and optional KB ingestion after a conversation.

**Tools (`app/agent/tools.py`):** Registry of tool definitions (name, description, input_schema, method, endpoint). All gateway-backed tools use a single base URL and API key. Only `memory_update` is INTERNAL (writes to agent memory, no gateway call). Executor builds URL from `settings.gateway_url + endpoint`, sends request with `X-API-Key`, returns response body or error string. SSRF guard blocks private/loopback IPs on URL arguments.

**Tool categories and gateway paths:**
- Calendar: `get_events`, `check_availability`, `create_event`, `update_event`, `delete_event`, `search_events` → `/calendar/*`
- Tasks: `get_task_lists`, `get_tasks`, `create_task_list`, `rename_task_list`, `create_task`, `update_task`, `delete_task` → `/tasks/*`
- Email: `list_emails`, `search_emails`, `get_email`, `draft_email`, `send_email`, `reply_to_email` → `/email/*`
- Notifications: `send_notification` → `/notify`
- Knowledge Base: `search_knowledge_base`, `get_kb_index`, `read_kb_source`, `list_kb_sources`, `delete_kb_source`, `ingest_text`, `ingest_url` → `/kb/*`. The KB has no Drive sync (removed 2026-10-18); content comes from console uploads and these ingest tools.
- Web Search: `web_search`, `fetch_url` → `/search/web*`
- Storage (Drive): `list_files`, `list_folders`, `create_folder`, `get_file_info`, `read_file`, `create_file`, `update_file`, `append_to_file`, `delete_file`, `move_file`, `copy_file`, `copy_file_from_github` → `/storage/*`
- GitHub: `list_repos`, `get_repo`, `list_issues`, `get_issue`, `create_issue`, `update_issue`, `add_issue_comment`, `list_prs`, `get_pr`, `add_pr_comment`, `create_pr`, `search_issues`, `get_github_file`, `search_code` → `/github/*`
- Google Sheets: `create_spreadsheet`, `get_spreadsheet_info`, `read_sheet`, `write_sheet`, `append_sheet_rows`, `clear_sheet_range` → `/sheets/*`
- Journal: `list_journal_entries`, `get_journal_entry`, `create_journal_entry`, `update_journal_entry`, `delete_journal_entry`, `list_journal_subcategories`, `get_journal_summary`, `export_journal`, `sync_journal_to_kb` → `/journal/*`. All take `category` (`career` | `personal`, default `career`) and `subcategory` (free-form). `sync_journal_to_kb` ingests a period's export into the KB under category `journal`, with a stable source_id per (category, subcategory, date range) so re-syncing replaces.
- Homelab: `homelab_status` (GET `/homelab/services` with fixed `compact=true` via `ToolDef.query`), `homelab_service`, `homelab_host`, `homelab_containers`, `restart_container`, `start_container`, `set_service_maintenance` → `/homelab/*`. `restart_container` and `start_container` are in `sensitive_tools`, so they always go through the pending_actions confirmation. Proxmox ACLs on the gateway's power token decide which VMIDs are allowed (108 never is).
- Automations (internal, `app/automations/tools.py`): `list_automations`, `create_automation`, `update_automation`, `delete_automation` (sensitive), `run_automation`. Chat-created automations are always `sazed_prompt` actions.
- Research jobs (internal, `app/jobs/tools.py`): `research` (question, depth quick|deep, optional model) queues a job for the sazed-worker (CT 110) and waits up to `RESEARCH_WAIT_SECONDS` (60); if still running it returns the job id and sets `notify`, so Pushover fires when it finishes. `job_status` fetches a job. Category `research`, co-selected with `web`.
- Memory (internal): `memory_update`

**Automations v2 (`app/automations/`, migration 003):** automations are rows (`automations`, `automation_runs`), not code. Each has a cron `trigger` + timezone, an `action` (`sazed_prompt`, or a `gateway_call` that is GET-only plus a small write allowlist in `models.GATEWAY_WRITE_ALLOWLIST`), and `outputs` (Pushover, journal, `skip_if_nothing`). `runner.py` wakes every 30s. `store.claim_due` row-locks due automations and advances `next_run_at` before running, so a slot never fires twice. Slots more than 15 min late are recorded as `missed`. Prompt runs use `run_turn(..., interactive=True, session_type="automation")` in a fresh session per run, so sensitive tools become pending approvals (run status `needs_approval`) instead of executing unattended; open the run's session in History to approve. REST: `/automations` (list/create), `/automations/{id}` (get with runs/patch/delete), `/automations/{id}/run` (202, background), `/automations/preview-schedule`. `AUTOMATIONS_SCHEDULER=false` stops firing without touching rows. A run that errors or stops at an approval gate always sends a Pushover alert, even with the Pushover output off (`AUTOMATION_ALERTS=false` to silence). Replaces the old script runner on CT 107.

**Worker jobs (`app/jobs/`, migration 004, plans/07):** `jobs` + `job_events` tables. `service.py` holds the rules: research prompts and depths (Gemini 3.8 Flash High via agy; the effort is part of the model id, so no `--effort`), model allowlists (`AGY_MODELS`, `CLAUDE_MODELS`, no Fable), daily caps per agent (`JOBS_DAILY_CAP_AGY` 20, `JOBS_DAILY_CAP_CLAUDE` 5), Pushover on finish. The worker uses `routers/worker.py`: `POST /worker/claim` (204 when idle; also expires silent jobs), `/worker/jobs/{id}/events` (heartbeat + cancel signal), and `/worker/jobs/{id}/finish`, authenticated by `X-Worker-Key` = `WORKER_KEY` (empty = 503, as on GCP). The console uses `routers/jobs.py`: `GET /jobs`, `GET /jobs/{id}` (with events), `POST /jobs/research`, `POST /jobs/{id}/cancel`.

**Routers:** `health.py` (GET /health), `chat.py` (POST /chat, GET /chat/stream), `conversations.py` (GET /conversations — accepts `?type=` filter, defaults to `chat`, pass `all` for every type; GET /conversations/{id}, POST /conversations/{id}/process, POST /conversations/archive), `memory.py` (GET /memory, PUT /memory, DELETE /memory/{id}), `kb.py` (GET /kb/stats, /kb/sources, /kb/files, POST /kb/search, DELETE /kb/files/{id}, DELETE /kb), `journal.py` (`/journal/*` proxy to api-gateway + `POST /journal/sync-kb` which ingests a period's markdown export into the KB via `/kb/ingest/text`), `tools.py` (GET /tools).

**Database:** `app/db.py` — asyncpg pool for sessions and agent_memory. Sessions store `context_summary` and `summarized_through` for context window management, and `session_type` (`chat` default; `think`, `automation`, `claude` for other callers) for listing/archival/process_session gating.

**Context window:** When a session exceeds `session_window_size` (default 15) messages, overflow is compressed into a rolling summary via Haiku and stored in the session row. The agent sees `[summary_pair] + recent_messages`.

**Session processing (`session.py`):** After each turn, `process_session` runs in parallel: fact extraction (Haiku → upsert to `agent_memory`), optional summary, optional structured KB entry ingested directly via the KB service's `/kb/ingest/text` (no Drive round-trip). Controlled by `session_kb_ingest_enabled` and `session_summarization` settings. Fact extraction and KB ingestion only run when `session_type == "chat"` — other session types are compress-only, so think/automation traffic doesn't pollute memory or the KB.

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
    chat.py            — POST /chat, POST /chat/stream (SSE), POST /chat/edit, POST /chat/edit/stream (truncate a past user message + everything after it, then resubmit)
    conversations.py   — GET /conversations, GET /conversations/{id}, POST /conversations/{id}/process
    memory.py          — GET /memory, PUT /memory, DELETE /memory/{id}
    kb.py              — KB proxy: stats, sources, search, delete
    tools.py           — GET /tools (agent tool registry)
chat.py                — CLI for testing (calls local /chat)
```

## Key Conventions

- **Ruff** for linting/formatting: line length 100, Python 3.11.
- All gateway calls via httpx, through one pooled `httpx.AsyncClient` (`app/http_client.py`, opened/closed by the FastAPI lifespan) — call `get_client()` rather than constructing a new client per request; single timeout (30s) per request.
- Config: `from app.config import settings`.
- Session and memory persistence: asyncpg when `DATABASE_URL` is set.
- Streaming uses SSE via `StreamingResponse` in `routers/chat.py`; events: `session`, `tool_start`, `tool_done`, `text_delta`, `done`. `run_turn_stream` accepts an optional `request` and polls `is_disconnected()` between turns so a cancelled client stops the loop before the next LLM call.
- Model selection: Haiku for turns 0–2, Sonnet from turn 3 onward (`_select_model` in `loop.py`).
- Prompt caching: system blocks and the last tool schema use `cache_control: ephemeral`.
