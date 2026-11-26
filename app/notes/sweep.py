"""Background sweep: file the inbox notes every settings.notes_sweep_seconds."""

import asyncio
import logging

from app.config import settings
from app.notes.service import sweep_once

logger = logging.getLogger(__name__)

_task: asyncio.Task | None = None


async def _loop() -> None:
    while True:
        await asyncio.sleep(max(60, settings.notes_sweep_seconds))
        try:
            result = await sweep_once()
            if result["filed"] or result["waiting"] or result["failed"]:
                logger.info(
                    "Notes sweep: %s filed, %s waiting, %s failed",
                    len(result["filed"]),
                    result["waiting"],
                    result["failed"],
                )
        except Exception:
            logger.exception("Notes sweep failed")


async def start() -> None:
    global _task
    if settings.notes_sweep_seconds <= 0 or not settings.gateway_url:
        logger.info("Notes sweep disabled")
        return
    _task = asyncio.create_task(_loop())
    logger.info("Notes sweep started (every %ss)", max(60, settings.notes_sweep_seconds))


async def stop() -> None:
    if _task is not None:
        _task.cancel()
