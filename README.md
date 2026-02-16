# Sazed

Personal AI agent. Orchestrates tools via the api-gateway to answer questions, manage calendar/tasks/email, and maintain persistent memory across conversations.

Part of a larger personal AI ecosystem — see `api-gateway` and `MY-AI` repos.

---

## Stack

- **FastAPI** (Python 3.11+) — REST API
- **Anthropic SDK** — agent loop with tool_use (Haiku + Sonnet)
- **asyncpg** — Postgres session/memory storage *(wired in Phase 3.3 — in-memory for now)*
- **Poetry** — dependency management
- **Docker** → GCP Cloud Run *(not deployed yet)*

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
```

Leave `API_KEY` and `DATABASE_URL` empty for local dev. Auth is disabled when `API_KEY` is unset.

---

## Running

```bash
# Dev server (hot reload)
poetry run uvicorn app.main:app --reload

# Chat CLI (in a separate terminal)
python chat.py
```

API docs available at `http://localhost:8000/docs`.

---

## Architecture

```
POST /chat
    │
    ▼
Agent loop (loop.py)
    ├── Loads memory → injects into system prompt
    ├── Loads session history
    ├── LLM call (Haiku or Sonnet)
    │     stop_reason: tool_use → execute tools → loop back (max 5 turns)
    │     stop_reason: end_turn → return response
    └── Saves messages to session store

Tools (tools.py) → all hit api-gateway with one base URL + one API key
Memory (memory.py) → structured facts injected into every system prompt
Session processing (session.py) → post-session fact extraction + summarization via Haiku
```

---

## Project Structure

```
app/
├── main.py              # FastAPI app, router mounts
├── config.py            # Settings (pydantic-settings)
├── dependencies.py      # API key auth
├── agent/
│   ├── loop.py          # Core agent loop, session accessors
│   ├── tools.py         # Tool registry + executor (20 tools)
│   ├── memory.py        # agent_memory store + prompt formatting
│   └── session.py       # Post-session fact extraction + summarization
└── routers/
    ├── health.py        # GET /health (public)
    ├── chat.py          # POST /chat
    ├── conversations.py # GET /conversations, GET /conversations/{id}, POST /conversations/{id}/process
    └── memory.py        # GET /memory, PUT /memory, DELETE /memory/{id}

chat.py                  # Local CLI for testing
```

---

## Tools

All tools call the api-gateway. One base URL, one API key.

| Category | Tools |
|----------|-------|
| Calendar | get_today, get_events, check_availability, create_event, update_event, delete_event |
| Tasks | get_upcoming_tasks, get_task_lists, create_task, update_task, delete_task |
| Email | get_recent_emails, get_unread_emails, search_emails, get_email, draft_email |
| Notify | send_notification |
| KB | search_knowledge_base *(stubbed until MY-AI deploys)* |
| Internal | memory_update *(writes directly to agent_memory)* |

---

## Commands

```bash
poetry install          # install dependencies
poetry run uvicorn app.main:app --reload   # dev server
ruff check app/         # lint
ruff format app/        # format
```

---

## Status

| Phase | Status |
|-------|--------|
| Bootstrap | ✅ Done |
| Tool registry | ✅ Done |
| Agent loop | ✅ Done |
| Structured memory | ✅ Done |
| Session processing | ✅ Done |
| DB wiring (asyncpg) | ⬜ Pending Cloud SQL setup |
| KB integration | ⬜ Pending MY-AI deploy |
| Deploy to Cloud Run | ⬜ Pending DB |
