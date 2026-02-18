# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**Sazed** is the personal AI agent: it runs an agent loop (LLM + tool calls), talks to the api-gateway for calendar, tasks, email, storage, notifications, and **knowledge-base search**, and keeps structured memory and conversation sessions. Part of a larger personal AI ecosystem — see `api-gateway` and `knowledge-base` repos (and `api-gateway/developmentplan.md`).

**Stack:** FastAPI (Python 3.11+), Anthropic SDK (tool_use), asyncpg for session/memory, Poetry. Deployed via Docker to GCP Cloud Run (workflow present but currently disabled).

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

**Entry point:** `app/main.py` — FastAPI app with lifespan (DB pool init/close), CORS, API key auth when `API_KEY` is set. Health is public; all other routes require `X-API-Key` when `API_KEY` is set.

**Configuration:** `app/config.py` — pydantic-settings from `.env`. Key vars: `GATEWAY_URL`, `GATEWAY_API_KEY`, `ANTHROPIC_API_KEY`, `DATABASE_URL`, `haiku_model`, `sonnet_model`.

**Flow:** `POST /chat` → agent loop in `app/agent/loop.py`: load memory and session history, call LLM (Haiku or Sonnet), on `tool_use` execute tools via `app/agent/tools.py` (all tools call the gateway except `memory_update`), repeat up to 5 turns, then return final response and persist messages. Session processing in `app/agent/session.py` runs fact extraction and summarization after a conversation.

**Tools (`app/agent/tools.py`):** Registry of tool definitions (name, description, input_schema, method, endpoint). All gateway-backed tools use a single base URL and API key. `search_knowledge_base` calls `POST /kb/search` on the gateway (proxied to the knowledge-base service when deployed). Only `memory_update` is INTERNAL (writes to agent memory, no gateway call). Executor builds URL from `settings.gateway_url + endpoint`, sends request with `X-API-Key`, returns response body or error string.

**Routers:** `health.py` (GET /health), `chat.py` (POST /chat), `conversations.py` (GET /conversations, GET /conversations/{id}, POST /conversations/{id}/process), `memory.py` (GET /memory, PUT /memory, DELETE /memory/{id}).

**Database:** `app/db.py` — asyncpg pool for sessions and agent_memory. Used when `DATABASE_URL` is set; otherwise in-memory.

## Module Layout

```
app/
  main.py              — FastAPI app, lifespan, router mounts
  config.py            — Settings (pydantic-settings)
  dependencies.py      — verify_api_key
  db.py                — asyncpg pool init/close/get
  agent/
    loop.py            — Agent loop: memory + session → LLM → tools → response
    tools.py           — TOOLS list, get_tool_schemas(), execute_tool(), _execute_internal()
    memory.py          — agent_memory store, prompt formatting, upsert_fact
    session.py         — load/save messages, post-session fact extraction + summary
  routers/
    health.py, chat.py, conversations.py, memory.py
chat.py                 — CLI for testing (calls local /chat)
```

## Key Conventions

- **Ruff** for linting/formatting: line length 100, Python 3.11.
- All gateway calls via httpx in `tools.py`; single timeout (30s) per request.
- Config: `from app.config import settings`.
- Session and memory persistence: asyncpg when `DATABASE_URL` is set.
