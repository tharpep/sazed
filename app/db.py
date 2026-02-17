"""PostgreSQL connection pool and schema initialization."""

import logging
from typing import Optional

import asyncpg

from app.config import settings

logger = logging.getLogger(__name__)

_pool: Optional[asyncpg.Pool] = None

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sessions (
    id            UUID PRIMARY KEY,
    created_at    TIMESTAMPTZ DEFAULT NOW(),
    last_activity TIMESTAMPTZ DEFAULT NOW(),
    message_count INT DEFAULT 0,
    processed_at  TIMESTAMPTZ,
    summary_kb_id UUID
);

CREATE TABLE IF NOT EXISTS messages (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id UUID REFERENCES sessions(id) ON DELETE CASCADE,
    role       TEXT NOT NULL,
    content    TEXT NOT NULL,
    timestamp  TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS messages_session_id_timestamp_idx
    ON messages (session_id, timestamp);

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
