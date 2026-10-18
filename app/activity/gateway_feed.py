"""Copy the gateway's own events (Pushovers, outages, disk and deploy alerts, MCP calls) into the
activity timeline, and prune old rows.

The gateway keeps them in an in-memory buffer (api-gateway app/activity.py) with a per-process
`boot` id and a sequence number. The last (boot, seq) stored is read back from activity_events
on startup, so a Sazed restart neither duplicates nor drops anything still in the buffer.
"""

import asyncio
import json
import logging
from datetime import datetime

import httpx

from app.config import settings
from app.db import get_pool
from app.http_client import get_client

logger = logging.getLogger(__name__)

POLL_SECONDS = 10
RETENTION_DAYS = 30
PRUNE_EVERY = 360  # polls: about once an hour

_task: asyncio.Task | None = None


async def _cursor() -> tuple[str | None, int]:
    rec = await get_pool().fetchrow(
        """
        SELECT refs->>'boot' AS boot, (refs->>'seq')::int AS seq FROM activity_events
        WHERE kind LIKE 'gw.%' AND refs ? 'seq' ORDER BY id DESC LIMIT 1
        """
    )
    return (rec["boot"], rec["seq"]) if rec else (None, 0)


async def _poll(boot: str | None, after: int) -> tuple[str | None, int]:
    resp = await get_client().get(
        f"{settings.gateway_url}/activity/events",
        params={"after": after, **({"boot": boot} if boot else {})},
        headers={"X-API-Key": settings.gateway_api_key},
        timeout=15.0,
    )
    resp.raise_for_status()
    data = resp.json()
    new_boot = data.get("boot")
    events = data.get("events") or []
    if not events:
        return new_boot, after if new_boot == boot else 0
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            for e in events:
                await conn.execute(
                    """
                    INSERT INTO activity_events (at, kind, title, status, trace, refs, detail,
                                                 duration_ms)
                    VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7::jsonb, $8)
                    """,
                    datetime.fromisoformat(e["at"]),
                    f"gw.{e['kind']}",
                    e["title"],
                    e.get("status") or "info",
                    f"gw:{new_boot}:{e['seq']}",
                    json.dumps({"boot": new_boot, "seq": e["seq"]}),
                    json.dumps(e.get("detail") or {}),
                    e.get("duration_ms"),
                )
    return new_boot, max(e["seq"] for e in events)


async def _prune() -> None:
    result = await get_pool().execute(
        f"DELETE FROM activity_events WHERE at < NOW() - INTERVAL '{RETENTION_DAYS} days'"
    )
    logger.info("Activity prune: %s", result)


async def _loop() -> None:
    boot, seq = await _cursor()
    tick = 0
    while True:
        try:
            boot, seq = await _poll(boot, seq)
        except httpx.HTTPError as e:
            logger.debug("Gateway activity poll failed: %s", e)
        except Exception:
            logger.warning("Gateway activity poll failed", exc_info=True)
        if tick % PRUNE_EVERY == 0:
            try:
                await _prune()
            except Exception:
                logger.warning("Activity prune failed", exc_info=True)
        tick += 1
        await asyncio.sleep(POLL_SECONDS)


async def start() -> None:
    global _task
    # Homelab only (where the worker runs), like the CI watcher; GCP has no gateway events.
    if not settings.worker_key or not settings.database_url:
        return
    try:
        await _cursor()
    except Exception:
        logger.exception("Gateway activity feed not started: activity table unavailable")
        return
    _task = asyncio.create_task(_loop())


async def stop() -> None:
    if _task is not None:
        _task.cancel()
