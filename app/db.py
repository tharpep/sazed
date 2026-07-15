"""PostgreSQL connection pool and schema initialization."""

import logging

import asyncpg

from app.config import settings
from app.migrations import run_migrations

logger = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None


async def init_pool() -> None:
    """Run pending migrations, then create the asyncpg pool."""
    global _pool

    if not settings.database_url:
        logger.warning("DATABASE_URL not set — skipping database init")
        return

    # asyncpg expects postgresql://, not postgresql+asyncpg://
    dsn = settings.database_url.replace("postgresql+asyncpg://", "postgresql://")

    # Run before the pool exists (see app/migrations/__init__.py) — failures
    # crash startup so Cloud Run keeps the previous revision live instead of
    # running half-applied schema, same philosophy api-gateway's migrations
    # already use.
    await run_migrations(dsn)

    logger.info("Connecting to PostgreSQL...")
    _pool = await asyncpg.create_pool(dsn=dsn, min_size=2, max_size=10)

    logger.info("Database pool ready")


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
