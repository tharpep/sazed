"""Lightweight forward-only SQL migrations runner.

Discovers `NNN_name.sql` files in this package, applies any not already recorded
in the `schema_migrations` tracker table, and runs each migration inside a
transaction. Idempotent: safe to call on every app startup.

Ported from api-gateway's app/migrations/__init__.py — same pattern, kept
identical so both repos' migration tooling behaves the same way.
"""

import logging
import re
from pathlib import Path

import asyncpg

logger = logging.getLogger(__name__)

_MIGRATIONS_DIR = Path(__file__).parent

_BOOTSTRAP_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

_FILENAME_RE = re.compile(r"^(\d{3})_([\w_]+)\.sql$")

# Arbitrary fixed key for a session-level advisory lock scoped to migrations —
# serializes concurrent run_migrations() calls (e.g. two deploys landing close
# together, or Cloud Run starting multiple instances at once) so a later
# instance waits for an earlier one to finish instead of racing it. Without
# this, two instances could both see the same migration as "pending" and one's
# INSERT INTO schema_migrations would hit a duplicate-key error mid-transaction
# and crash startup for no real reason — the schema change itself is idempotent,
# only the bookkeeping row is not.
_MIGRATION_LOCK_KEY = 472819335


def _discover() -> list[tuple[int, str, Path]]:
    out: list[tuple[int, str, Path]] = []
    for path in sorted(_MIGRATIONS_DIR.glob("*.sql")):
        m = _FILENAME_RE.match(path.name)
        if not m:
            logger.warning("Ignoring migration file with bad name: %s", path.name)
            continue
        out.append((int(m.group(1)), m.group(2), path))
    return out


async def run_migrations(dsn: str) -> None:
    """Apply any pending migrations.

    Connects with a one-shot asyncpg connection (not the app pool) so this can
    run before the pool exists and so the connection state is fully isolated.
    """
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute("SELECT pg_advisory_lock($1)", _MIGRATION_LOCK_KEY)
        try:
            await conn.execute(_BOOTSTRAP_SQL)
            applied: set[int] = {
                r["version"]
                for r in await conn.fetch("SELECT version FROM schema_migrations")
            }
            pending = [m for m in _discover() if m[0] not in applied]
            if not pending:
                logger.info("Migrations up to date (%d applied)", len(applied))
                return
            for version, name, path in pending:
                sql = path.read_text()
                logger.info("Applying migration %03d: %s", version, name)
                async with conn.transaction():
                    await conn.execute(sql)
                    await conn.execute(
                        "INSERT INTO schema_migrations (version, name) VALUES ($1, $2) "
                        "ON CONFLICT (version) DO NOTHING",
                        version,
                        name,
                    )
            logger.info("Migrations complete (%d applied)", len(applied) + len(pending))
        finally:
            await conn.execute("SELECT pg_advisory_unlock($1)", _MIGRATION_LOCK_KEY)
    finally:
        await conn.close()
