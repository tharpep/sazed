"""Phone notices. Sazed names the kind and the ids it has; the gateway's rule for that kind
(api-gateway app/notices.py) sets priority, expiry, the button and the console screen it opens.
No rule lives here or at a call site."""

import logging
from typing import Literal

import httpx

from app.config import settings
from app.http_client import get_client

logger = logging.getLogger(__name__)

NoticeKind = Literal[
    "question",
    "permission",
    "merge_call",
    "run_failed",
    "spend_warning",
    "job_done",
    "routine",
]

# Pushover's own limits; the gateway clamps further for a phone's line.
_TITLE_MAX = 250
_MESSAGE_MAX = 1024


async def notify_simple(
    kind: NoticeKind, title: str, body: str, refs: dict[str, str] | None = None
) -> bool:
    """One Pushover message: a short title and a sentence or two (the gateway trims anything
    longer). `refs` are the ids it can link to: job, card, automation. True once the gateway
    took it."""
    payload = {
        "kind": kind,
        "title": title[:_TITLE_MAX],
        "message": body[:_MESSAGE_MAX] or "(no details)",
        "refs": refs or {},
    }
    try:
        resp = await get_client().post(
            f"{settings.gateway_url}/notify",
            json=payload,
            headers={"X-API-Key": settings.gateway_api_key},
            timeout=30.0,
        )
    except httpx.HTTPError:
        logger.warning("Notification failed: %s", title, exc_info=True)
        return False
    if not resp.is_success:
        logger.warning("Notification refused (%s): %s", resp.status_code, title)
    return resp.is_success
