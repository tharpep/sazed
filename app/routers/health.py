"""Health check — public endpoint."""

import logging

from fastapi import APIRouter, Response

from app.config import settings
from app.db import get_pool

router = APIRouter(tags=["health"])
logger = logging.getLogger(__name__)


@router.get("/health")
async def health(response: Response):
    """Report service health, including DB reachability.

    Every route sazed serves depends on the DB (sessions, memory) or the
    gateway, but this previously always returned 200 regardless — so a
    degraded DB pool left Cloud Run routing traffic to a broken instance
    instead of cycling it. Returns 503 when DATABASE_URL is set but
    unreachable; a missing DATABASE_URL is a valid local-dev state, not
    a failure.
    """
    if not settings.database_url:
        return {"status": "ok", "service": "sazed", "db": "not_configured"}

    try:
        await get_pool().fetchval("SELECT 1")
        return {"status": "ok", "service": "sazed", "db": "ok"}
    except Exception:
        logger.exception("Health check: database ping failed")
        response.status_code = 503
        return {"status": "error", "service": "sazed", "db": "error"}
