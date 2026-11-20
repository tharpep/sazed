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
import time

from app.board import dispatch
from app.config import settings
from app.jobs import service, store
from app.learning import service as learning

logger = logging.getLogger(__name__)

_task: asyncio.Task | None = None


def hold_s() -> int:
    """How long to wait after start-up or a failed pass. Beats sent while this process or its
    database was down were lost, so a run only counts as silent after a full grace of listening."""
    return max(settings.job_heartbeat_grace_s, settings.dispatch_heartbeat_grace_s)


async def reap_once() -> bool:
    """One pass; each kind fails on its own. False when either kind failed."""
    ok = True
    try:
        for job in await store.expire_stale(silent_only=True):
            logger.warning("Reaped job %s: %s", job["id"], job.get("error"))
            await service.on_finished(job)
    except Exception:
        ok = False
        logger.exception("Reaping worker jobs failed")
    try:
        for row in await dispatch.reap_silent(settings.dispatch_heartbeat_grace_s):
            logger.warning("Reaped dispatch task %s/%s: %s", row["plan"], row["task"], row["line"])
            await learning.record_dispatch(row["plan"], row["task"], row["state"], row["meta"])
    except Exception:
        ok = False
        logger.exception("Reaping dispatch tasks failed")
    return ok


class Gate:
    """Holds reaping for hold_s() after start-up and after each failed pass."""

    def __init__(self, now: float):
        self.hold_until = now + hold_s()

    async def tick(self, now: float) -> bool:
        """Runs a pass unless held; True when a pass ran."""
        if now < self.hold_until:
            return False
        if not await reap_once():
            self.hold_until = now + hold_s()
        return True


async def _loop() -> None:
    gate = Gate(time.monotonic())
    while True:
        await asyncio.sleep(settings.reaper_interval_s)
        await gate.tick(time.monotonic())


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
