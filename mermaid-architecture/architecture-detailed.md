# Sazed — Detailed Architecture

Specific structure: entry point, routers, agent loop, tools, and database.

```mermaid
flowchart TB
    subgraph Entry["Entry & Config"]
        Main["app/main.py\nFastAPI, lifespan: init_pool / close_pool"]
        Config["app/config.py\nSettings: gateway_url, gateway_api_key\nanthropic, haiku/sonnet model, database_url"]
        Deps["app/dependencies.py\nverify_api_key"]
    end

    subgraph Routers["Routers"]
        Health["health.py\nGET /health (public)"]
        Chat["chat.py\nPOST /chat → run_turn"]
        Conversations["conversations.py\nGET /conversations, GET /conversations/{id}\nPOST /conversations/{id}/process"]
        MemoryRouter["memory.py\nGET /memory, PUT /memory, DELETE /memory/{id}"]
    end

    subgraph Agent["Agent (app/agent)"]
        Loop["loop.py\nrun_turn(session_id, user_message)\nLoad history → append user → LLM with tools\nmax MAX_TURNS, persist each message"]
        Tools["tools.py\nTOOLS (ToolDef), get_tool_schemas()\nexecute_tool() → gateway or _execute_internal"]
        Memory["memory.py\nload_memory(), upsert_fact(), format_for_prompt()\nInjected into system prompt"]
        Session["session.py\nPost-session: fact extraction + summarization\n→ agent_memory, → gateway /kb/ingest"]
    end

    subgraph DB["Database (app/db.py)"]
        Sessions["sessions\nid, created_at, last_activity, message_count, processed_at, summary_kb_id"]
        Messages["messages\nsession_id, role, content, timestamp"]
        AgentMemory["agent_memory\nfact_type, key, value, source, confidence\nUNIQUE(fact_type, key)"]
    end

    subgraph Gateway["api-gateway (all tools)"]
        Cal["/calendar/*"]
        Tasks["/tasks/*"]
        Email["/email/*"]
        Notify["/notify"]
        KB["/kb/search"]
    end

    Main --> Config
    Main --> Deps
    Main --> Routers
    Chat --> Loop
    Loop --> Tools
    Loop --> Memory
    Loop --> Sessions
    Loop --> Messages
    Tools --> Cal
    Tools --> Tasks
    Tools --> Email
    Tools --> Notify
    Tools --> KB
    Tools --> Memory
    Memory --> AgentMemory
    Conversations --> Loop
    Conversations --> Session
    Session --> AgentMemory
    Session --> KB
```

**Key files:**

| File | Role |
|------|------|
| `app/main.py` | Mounts health, chat, conversations, memory; lifespan manages pool |
| `app/agent/loop.py` | run_turn: load session, build system prompt (with memory), LLM with tool_use, execute_tool, persist messages |
| `app/agent/tools.py` | ToolDef list (calendar, tasks, email, notify, search_knowledge_base, memory_update); execute_tool → httpx to gateway or internal |
| `app/agent/memory.py` | load_memory, upsert_fact (confidence-based), format_for_prompt for system prompt |
| `app/agent/session.py` | Post-session processing: extract facts, summarize, ingest summary to KB |
| `app/db.py` | asyncpg pool, sessions / messages / agent_memory schema |
