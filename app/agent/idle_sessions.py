"""Idle-session processing: run process_session on chats once they go quiet.

process_session (fact extraction, summary, KB ingest, reflection, procedure proposals) used to
run only from the manual /process endpoint and from archiving after 30 idle days, so on a young
database nothing was ever learned. Every POLL_SECONDS this picks up chat sessions idle for
settings.idle_session_hours that were never processed, processes them one at a time, and stamps
processed_at. A session is processed once; activity after that is left to archiving, which
skips sessions already processed so the KB doesn't get a second summary.
Runs only where the worker is configured (WORKER_KEY set), like the CI watcher, so not on GCP.
"""

import asyncio
import logging

from app.agent.loop import get_session
from app.agent.session import mark_processed, process_session
from app.config import settings
from app.db import get_pool

logger = logging.getLogger(__name__)

POLL_SECONDS = 600
BATCH_SIZE = 5  # sessions per tick, so a backfill spreads over a few ticks
_task: asyncio.Task | None = None


async def _due_sessions() -> list[dict]:
    rows = await get_pool().fetch(
        """
        SELECT id, created_at, last_activity FROM sessions
        WHERE session_type = 'chat'
          AND processed_at IS NULL
          AND message_count >= 2
          AND last_activity < NOW() - make_interval(hours => $1)
        ORDER BY last_activity
        LIMIT $2
        """,
        settings.idle_session_hours,
        BATCH_SIZE,
    )
    return [dict(r) for r in rows]


async def _process(row: dict) -> None:
    session_id = str(row["id"])
    messages = await get_session(session_id)
    if not messages:
        # Nothing to learn from; stamp it so the sweep doesn't keep picking it up.
        await mark_processed(session_id)
        return
    result = await process_session(
        session_id,
        messages,
        session_dt=row["last_activity"],
        session_start=row["created_at"],
        session_type="chat",
    )
    await mark_processed(session_id)
    logger.info(
        "Processed idle session %s: %s fact(s), procedure=%s, kb=%s",
        session_id,
        result.get("facts_extracted"),
        result.get("procedure_proposed"),
        result.get("kb_ingested"),
    )


async def _loop() -> None:
    while True:
        try:
            for row in await _due_sessions():
                try:
                    await _process(row)
                except Exception:
                    # Left unstamped, so the next tick retries it.
                    logger.exception("Processing idle session %s failed", row["id"])
        except Exception:
            logger.exception("Idle-session sweep failed")
        await asyncio.sleep(POLL_SECONDS)


async def start() -> None:
    global _task
    if not settings.worker_key or not settings.database_url:
        logger.info("Idle-session processing disabled (not the homelab)")
        return
    _task = asyncio.create_task(_loop())
    logger.info(
        "Idle-session processing started (every %ss, idle %sh)",
        POLL_SECONDS,
        settings.idle_session_hours,
    )


async def stop() -> None:
    if _task is not None:
        _task.cancel()
