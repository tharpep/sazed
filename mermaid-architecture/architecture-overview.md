# Sazed — Architecture Overview

High-level view for quickly understanding the codebase.

```mermaid
flowchart LR
    subgraph Client["Client"]
        User["User / Frontend"]
    end

    subgraph Sazed["Sazed (agent)"]
        API["FastAPI"]
        Chat["POST /chat"]
        Loop["Agent loop"]
        Tools["Tools"]
        Memory["Memory"]
        DB[(PostgreSQL)]
    end

    subgraph Gateway["api-gateway"]
        GW["Calendar, Tasks, Email\nNotify, KB, Storage, AI"]
    end

    User --> Chat
    Chat --> Loop
    Loop --> Tools
    Loop --> Memory
    Tools --> GW
    Memory --> DB
    Loop --> DB
```

**In one sentence:** FastAPI agent that runs an LLM loop with tool use, persists sessions and structured memory in Postgres, and calls the api-gateway for calendar, tasks, email, KB, and notifications.
