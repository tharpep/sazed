"""Live fan-out for the Activity page.

One dedicated Postgres connection LISTENs on 'activity' (new timeline rows, sent by the
activity_notify trigger) and 'activity_step' (job steps). Each open console page holds a
subscriber queue; nothing is buffered when nobody is watching. Notifications fire on commit, so
writes from any code path (or a manual psql insert) show up here.
"""

import asyncio
import json
import logging
from typing import Any

import asyncpg

from app.activity import service
from app.config import settings
from app.db import get_pool

logger = logging.getLogger(__name__)

QUEUE_SIZE = 500
_subscribers: set[asyncio.Queue] = set()
_task: asyncio.Task | None = None


def subscribe() -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_SIZE)
    _subscribers.add(q)
    return q


def unsubscribe(q: asyncio.Queue) -> None:
    _subscribers.discard(q)


def _publish(kind: str, data: dict[str, Any]) -> None:
    for q in list(_subscribers):
        try:
            q.put_nowait((kind, data))
        except asyncio.QueueFull:
            # A stalled client: drop it rather than grow memory. Its stream ends (the router
            # checks is_subscribed) and the page reconnects, catching up from its last id.
            _subscribers.discard(q)


def is_subscribed(q: asyncio.Queue) -> bool:
    return q in _subscribers


async def _enrich(row: dict[str, Any]) -> dict[str, Any]:
    """The notify payload has no detail; model calls need theirs for cost and tokens."""
    if row.get("kind") == "llm":
        detail = await get_pool().fetchval(
            "SELECT detail FROM activity_events WHERE id = $1", row["id"]
        )
        detail = json.loads(detail) if isinstance(detail, str) else detail
        row["cost_usd"] = service.cost_usd(detail)
        if detail:
            row["tokens"] = (detail.get("input_tokens") or 0) + (detail.get("output_tokens") or 0)
    return row


def _on_activity(_conn, _pid, _channel, payload: str) -> None:
    if not _subscribers:
        return
    try:
        row = json.loads(payload)
    except ValueError:
        return

    async def deliver() -> None:
        try:
            _publish("activity", await _enrich(row))
        except Exception:
            logger.warning("Activity enrich failed", exc_info=True)
            _publish("activity", row)

    asyncio.get_running_loop().create_task(deliver())


def _on_step(_conn, _pid, _channel, payload: str) -> None:
    if not _subscribers:
        return
    try:
        _publish("step", json.loads(payload))
    except ValueError:
        return


async def _listen_forever() -> None:
    dsn = settings.database_url.replace("postgresql+asyncpg://", "postgresql://")
    while True:
        conn = None
        try:
            conn = await asyncpg.connect(dsn)
            await conn.add_listener("activity", _on_activity)
            await conn.add_listener("activity_step", _on_step)
            logger.info("Activity listener connected")
            while not conn.is_closed():
                await asyncio.sleep(15)
                await conn.execute("SELECT 1")  # notices a dead connection
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Activity listener lost its connection; retrying", exc_info=True)
        finally:
            if conn is not None and not conn.is_closed():
                await conn.close()
        await asyncio.sleep(5)


async def start() -> None:
    global _task
    if not settings.database_url:
        return
    _task = asyncio.create_task(_listen_forever())


async def stop() -> None:
    if _task is not None:
        _task.cancel()
