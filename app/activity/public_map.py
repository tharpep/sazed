"""Public architecture map for the portfolio (pryce-tharpe.dev/homelab).

Every minute: take the gateway's public-safe map (GET /homelab/map/public: public nodes and
lines, statuses, 7-day uptime), add each line's traffic for the last 15 minutes and a few
24-hour counts, and push the snapshot to Upstash Redis over HTTPS. The portfolio reads it with a
read-only token. Nothing on the homelab is reachable from the internet; if the homelab goes
down the snapshot simply stops updating, and the portfolio shows it as offline.

Only counts leave the homelab, never event titles (they carry chat text) or anything that
identifies hosts. Disabled unless PUBLIC_MAP_REDIS_URL and PUBLIC_MAP_REDIS_TOKEN are set.
"""

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import Any

import httpx

from app.activity.map_report import signal_matches
from app.config import settings
from app.db import get_pool
from app.http_client import get_client

logger = logging.getLogger(__name__)

PUSH_SECONDS = 60
TRAFFIC_MINUTES = 15
SNAPSHOT_VERSION = 1

_task: asyncio.Task | None = None


async def _gateway(path: str) -> dict[str, Any]:
    resp = await get_client().get(
        f"{settings.gateway_url}{path}",
        headers={"X-API-Key": settings.gateway_api_key},
        timeout=30.0,
    )
    resp.raise_for_status()
    return resp.json()


async def _traffic(edges: list[dict[str, Any]], keys: set[str]) -> dict[str, int]:
    """Events per public line in the last TRAFFIC_MINUTES, using the map's signals."""
    rows = await get_pool().fetch(
        f"""
        SELECT kind, title, refs->>'agent' AS agent FROM activity_events
        WHERE at > NOW() - INTERVAL '{TRAFFIC_MINUTES} minutes'
        """
    )
    counts = dict.fromkeys(keys, 0)
    for e in edges:
        key = f"{e['from']}>{e['to']}"
        if key not in counts:
            continue
        for r in rows:
            if any(signal_matches(s, r["kind"], r["title"], r["agent"]) for s in e["signals"]):
                counts[key] += 1
    return counts


async def _stats_24h() -> dict[str, int]:
    pool = get_pool()
    rec = await pool.fetchrow(
        """
        SELECT
          count(*) FILTER (WHERE kind = 'chat.user') AS messages,
          count(*) FILTER (WHERE kind = 'llm') AS model_calls,
          count(*) FILTER (WHERE kind = 'tool') AS tool_calls,
          count(*) FILTER (WHERE kind = 'job.pr') AS prs,
          count(*) FILTER (WHERE kind = 'automation' AND status <> 'running') AS automation_runs,
          count(*) FILTER (WHERE kind = 'gw.notify') AS notifications
        FROM activity_events WHERE at > NOW() - INTERVAL '24 hours'
        """
    )
    jobs = await pool.fetchval(
        "SELECT count(*) FROM jobs WHERE created_at > NOW() - INTERVAL '24 hours'"
    )
    return {**{k: int(v) for k, v in dict(rec).items()}, "jobs": int(jobs or 0)}


async def build_snapshot() -> dict[str, Any]:
    public = await _gateway("/homelab/map/public")
    full = await _gateway("/homelab/map")
    keys = {f"{e['from']}>{e['to']}" for e in public["edges"]}
    return {
        "version": SNAPSHOT_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "map": public,
        "traffic_15m": await _traffic(full["edges"], keys),
        "stats_24h": await _stats_24h(),
    }


async def _push(snapshot: dict[str, Any]) -> None:
    resp = await get_client().post(
        f"{settings.public_map_redis_url.rstrip('/')}/set/{settings.public_map_key}",
        content=json.dumps(snapshot, separators=(",", ":")),
        headers={"Authorization": f"Bearer {settings.public_map_redis_token}"},
        timeout=20.0,
    )
    resp.raise_for_status()


async def _loop() -> None:
    while True:
        try:
            await _push(await build_snapshot())
        except httpx.HTTPError as e:
            logger.warning("Public map push failed: %s", e)
        except Exception:
            logger.warning("Public map push failed", exc_info=True)
        await asyncio.sleep(PUSH_SECONDS)


async def start() -> None:
    global _task
    if not (settings.public_map_redis_url and settings.public_map_redis_token):
        logger.info("Public map publisher off (no PUBLIC_MAP_REDIS_URL/TOKEN)")
        return
    _task = asyncio.create_task(_loop())
    logger.info("Public map publisher started (every %ss)", PUSH_SECONDS)


async def stop() -> None:
    if _task is not None:
        _task.cancel()
