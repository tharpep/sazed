"""PostgreSQL connection pool and schema initialization."""

import logging

import asyncpg

from app.config import settings

logger = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sessions (
    id                 UUID PRIMARY KEY,
    created_at         TIMESTAMPTZ DEFAULT NOW(),
    last_activity      TIMESTAMPTZ DEFAULT NOW(),
    message_count      INT DEFAULT 0,
    processed_at       TIMESTAMPTZ,
    summary_kb_id      UUID,
    context_summary    TEXT,
    summarized_through INT DEFAULT 0
);

-- Add columns for existing deployments that predate context windowing
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS context_summary TEXT;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS summarized_through INT DEFAULT 0;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS session_type TEXT DEFAULT 'chat';
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS title TEXT;

CREATE TABLE IF NOT EXISTS messages (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id UUID REFERENCES sessions(id) ON DELETE CASCADE,
    role       TEXT NOT NULL,
    content    TEXT NOT NULL,
    timestamp  TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS messages_session_id_timestamp_idx
    ON messages (session_id, timestamp);

CREATE TABLE IF NOT EXISTS archived_sessions (
    id                 UUID PRIMARY KEY,
    created_at         TIMESTAMPTZ,
    last_activity      TIMESTAMPTZ,
    message_count      INT,
    processed_at       TIMESTAMPTZ,
    summary_kb_id      UUID,
    context_summary    TEXT,
    summarized_through INT,
    session_type       TEXT DEFAULT 'chat',
    archived_at        TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE archived_sessions ADD COLUMN IF NOT EXISTS session_type TEXT DEFAULT 'chat';

CREATE TABLE IF NOT EXISTS archived_messages (
    id         UUID PRIMARY KEY,
    session_id UUID NOT NULL,
    role       TEXT NOT NULL,
    content    TEXT NOT NULL,
    timestamp  TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS archived_messages_session_id_idx
    ON archived_messages (session_id);

CREATE TABLE IF NOT EXISTS agent_memory (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    fact_type   TEXT NOT NULL,
    key         TEXT NOT NULL,
    value       TEXT NOT NULL,
    source      TEXT,
    confidence  FLOAT DEFAULT 1.0,
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    updated_at  TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (fact_type, key)
);

-- bi-temporal columns on the live table (idempotent)
ALTER TABLE agent_memory ADD COLUMN IF NOT EXISTS valid_from    TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE agent_memory ADD COLUMN IF NOT EXISTS last_confirmed TIMESTAMPTZ DEFAULT NOW();

-- append-only history of superseded values
CREATE TABLE IF NOT EXISTS agent_memory_history (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    fact_type     TEXT NOT NULL,
    key           TEXT NOT NULL,
    value         TEXT NOT NULL,
    confidence    FLOAT,
    source        TEXT,
    valid_from    TIMESTAMPTZ,
    valid_to      TIMESTAMPTZ DEFAULT NOW(),
    superseded_by TEXT
);
CREATE INDEX IF NOT EXISTS agent_memory_history_key_idx ON agent_memory_history (fact_type, key);

CREATE TABLE IF NOT EXISTS action_logs (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id    UUID REFERENCES sessions(id) ON DELETE CASCADE,
    timestamp     TIMESTAMPTZ DEFAULT NOW(),
    tool_name     TEXT NOT NULL,
    input         JSONB,
    output        TEXT,
    status        TEXT NOT NULL,
    error_message TEXT,
    duration_ms   INT
);

CREATE INDEX IF NOT EXISTS action_logs_session_id_idx ON action_logs (session_id);
CREATE INDEX IF NOT EXISTS action_logs_timestamp_idx  ON action_logs (timestamp DESC);

CREATE TABLE IF NOT EXISTS pending_actions (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id  UUID REFERENCES sessions(id) ON DELETE CASCADE,
    tool_name   TEXT NOT NULL,
    tool_input  JSONB NOT NULL,
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    status      TEXT NOT NULL DEFAULT 'pending'
);
CREATE INDEX IF NOT EXISTS pending_actions_session_idx ON pending_actions (session_id, status);

CREATE TABLE IF NOT EXISTS procedures (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            TEXT NOT NULL,
    trigger_desc    TEXT NOT NULL,
    trigger_keywords TEXT[],
    steps           JSONB NOT NULL,
    status          TEXT NOT NULL DEFAULT 'proposed',
    source_session  UUID,
    use_count       INT DEFAULT 0,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    last_used_at    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS procedures_status_idx ON procedures (status);

CREATE TABLE IF NOT EXISTS llm_calls (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id         UUID REFERENCES sessions(id) ON DELETE CASCADE,
    timestamp          TIMESTAMPTZ DEFAULT NOW(),
    turn               INT,
    model              TEXT NOT NULL,
    purpose            TEXT,
    input_tokens       INT,
    output_tokens      INT,
    cache_read_tokens  INT,
    cache_write_tokens INT,
    duration_ms        INT
);
CREATE INDEX IF NOT EXISTS llm_calls_session_idx ON llm_calls (session_id);
CREATE INDEX IF NOT EXISTS llm_calls_timestamp_idx ON llm_calls (timestamp DESC);
"""


async def init_pool() -> None:
    """Create the asyncpg pool and initialize the schema."""
    global _pool

    if not settings.database_url:
        logger.warning("DATABASE_URL not set — skipping database init")
        return

    # asyncpg expects postgresql://, not postgresql+asyncpg://
    dsn = settings.database_url.replace("postgresql+asyncpg://", "postgresql://")

    logger.info("Connecting to PostgreSQL...")
    _pool = await asyncpg.create_pool(dsn=dsn, min_size=2, max_size=10)

    async with _pool.acquire() as conn:
        await conn.execute(_SCHEMA_SQL)

    logger.info("Database pool ready and schema initialized")


def get_pool() -> asyncpg.Pool:
    """Return the active pool. Raises if not initialized."""
    if _pool is None:
        raise RuntimeError("Database pool not initialized — call init_pool() first")
    return _pool


async def close_pool() -> None:
    """Close the pool on shutdown."""
    global _pool
    if _pool:
        await _pool.close()
        _pool = None
        logger.info("Database pool closed")
