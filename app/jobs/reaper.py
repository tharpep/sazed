"""Stuck-run reaper (seam 4): every REAPER_INTERVAL_S, end worker jobs and dispatch tasks that
stopped sending heartbeats.

Before this, a silent job was expired only when some worker called /worker/claim, and a dispatch
task whose agent died showed as running forever. Each pass is one statement per kind that is safe
to run from several processes at once (see store.expire_stale and dispatch.reap_silent), so the
GCP and homelab apps, or two of either, never reap a run twice and need no lock between them.
Runs wherever there is a database, unlike the CI watcher, because dispatch can mirror to either.
"""

import asyncio
import logging

from app.board import dispatch
from app.config import settings
from app.jobs import service, store

logger = logging.getLogger(__name__)

_task: asyncio.Task | None = None


async def reap_once() -> None:
    """One pass; each kind fails on its own."""
    try:
        for job in await store.expire_stale():
            logger.warning("Reaped job %s: %s", job["id"], job.get("error"))
            await service.on_finished(job)
    except Exception:
        logger.exception("Reaping worker jobs failed")
    try:
        for row in await dispatch.reap_silent(settings.dispatch_heartbeat_grace_s):
            logger.warning("Reaped dispatch task %s/%s: %s", row["plan"], row["task"], row["line"])
    except Exception:
        logger.exception("Reaping dispatch tasks failed")


async def _loop() -> None:
    while True:
        await reap_once()
        await asyncio.sleep(settings.reaper_interval_s)


async def start() -> None:
    global _task
    if not settings.database_url:
        logger.info("Stuck-run reaper disabled (no database)")
        return
    _task = asyncio.create_task(_loop())
    logger.info("Stuck-run reaper started (every %ss)", settings.reaper_interval_s)


async def stop() -> None:
    if _task is not None:
        _task.cancel()
