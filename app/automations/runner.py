"""Automation scheduler and executor.

The scheduler wakes every 30s, claims due rows (store.claim_due advances next_run_at first, so a
slot never fires twice), and runs each in its own task. A slot more than CATCH_UP late (Sazed
was down) is recorded as 'missed' instead of firing a burst of stale runs.

Prompt actions run through the normal agent loop with interactive=True in a fresh session per run:
sensitive tools become pending approvals (status 'needs_approval') rather than running unattended,
and a fresh session means no later prompt can accidentally approve an earlier run's action.
"""

import asyncio
import logging
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from app.automations import store
from app.config import settings
from app.db import get_pool
from app.http_client import get_client as get_http_client

logger = logging.getLogger(__name__)

POLL_SECONDS = 30
CATCH_UP = timedelta(minutes=15)
NOTHING = "NOTHING"
_NOTHING_HINT = (
    "\n\nIf there is nothing worth telling me, reply with exactly NOTHING and nothing else."
)

_task: asyncio.Task | None = None
_running: set[uuid.UUID] = set()  # automation ids with a run in flight
_run_tasks: set[asyncio.Task] = set()  # strong refs so runs aren't garbage-collected


# ---------------------------------------------------------------- execution


async def trigger_run(automation: dict[str, Any], triggered_by: str) -> uuid.UUID | None:
    """Start a run in the background. Returns the run id, or None if one is already running."""
    if automation["id"] in _running:
        return None
    run_id = await store.start_run(automation["id"], triggered_by)
    _running.add(automation["id"])
    task = asyncio.create_task(_execute(automation, run_id))
    _run_tasks.add(task)
    task.add_done_callback(_run_tasks.discard)
    return run_id


async def _execute(automation: dict[str, Any], run_id: uuid.UUID) -> None:
    name = automation["name"]
    outputs = automation["outputs"] or {}
    try:
        if automation["action"]["type"] == "sazed_prompt":
            status, text, session_id = await _run_prompt(automation)
        else:
            status, text = await _run_gateway_call(automation["action"])
            session_id = None
        delivered: list[str] = []
        if status in ("success", "needs_approval"):
            delivered = await _deliver(
                name, outputs, text, needs_approval=status == "needs_approval"
            )
        await store.finish_run(
            run_id, status, output=text[:8000], session_id=session_id, delivered=delivered
        )
        logger.info("Automation '%s' finished: %s", name, status)
    except Exception as e:
        logger.exception("Automation '%s' failed", name)
        await store.finish_run(run_id, "error", error=f"{type(e).__name__}: {e}"[:2000])
        if outputs.get("pushover"):
            await _notify(f"Automation failed: {name}", str(e)[:500], priority=0)
    finally:
        _running.discard(automation["id"])


async def _run_prompt(automation: dict[str, Any]) -> tuple[str, str, uuid.UUID]:
    from app.agent.loop import run_turn  # lazy: the agent loop imports the tool registry

    outputs = automation["outputs"] or {}
    prompt = automation["action"]["prompt"]
    if outputs.get("skip_if_nothing"):
        prompt += _NOTHING_HINT
    session_id, text = await run_turn(
        None,
        prompt,
        timezone=automation["trigger"].get("timezone"),
        interactive=True,  # keeps the approval gate on: nothing sensitive runs unattended
        session_type="automation",
    )
    sid = uuid.UUID(session_id)
    pending = await get_pool().fetchval(
        "SELECT COUNT(*) FROM pending_actions WHERE session_id = $1 AND status = 'pending'", sid
    )
    if pending:
        return "needs_approval", text, sid
    if outputs.get("skip_if_nothing") and text.strip().rstrip(".").upper() == NOTHING:
        return "skipped", text, sid
    return "success", text, sid


async def _run_gateway_call(action: dict[str, Any]) -> tuple[str, str]:
    resp = await get_http_client().request(
        action["method"],
        f"{settings.gateway_url}{action['path']}",
        json=action.get("body") if action["method"] != "GET" else None,
        headers={"X-API-Key": settings.gateway_api_key},
        timeout=120.0,
    )
    if not resp.is_success:
        raise RuntimeError(f"gateway returned {resp.status_code}: {resp.text[:300]}")
    return "success", resp.text


# ---------------------------------------------------------------- outputs


def _plain(text: str) -> str:
    """Markdown → plain text for Pushover (it renders neither markdown nor long text)."""
    text = re.sub(r"\*{1,3}(.+?)\*{1,3}", r"\1", text)
    text = re.sub(r"`(.+?)`", r"\1", text)
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*[-*+]\s+", "• ", text, flags=re.MULTILINE)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


async def _notify(title: str, message: str, priority: int) -> bool:
    try:
        resp = await get_http_client().post(
            f"{settings.gateway_url}/notify",
            json={
                "title": title[:250],
                "message": message[:1024] or "(empty)",
                "priority": priority,
            },
            headers={"X-API-Key": settings.gateway_api_key},
            timeout=30.0,
        )
        return resp.is_success
    except httpx.HTTPError:
        logger.warning("Pushover delivery failed", exc_info=True)
        return False


async def _deliver(name: str, outputs: dict, text: str, needs_approval: bool) -> list[str]:
    delivered: list[str] = []
    if pushover := outputs.get("pushover"):
        title = pushover.get("title") or name
        if needs_approval:
            title = f"Needs approval: {title}"
        if await _notify(title, _plain(text), pushover.get("priority", 0)):
            delivered.append("pushover")
    if journal := outputs.get("journal"):
        try:
            resp = await get_http_client().post(
                f"{settings.gateway_url}/journal/entries",
                json={
                    "category": journal.get("category", "personal"),
                    "subcategory": journal.get("subcategory", "Automations"),
                    "title": name,
                    "body": text,
                    "tags": ["automation"],
                },
                headers={"X-API-Key": settings.gateway_api_key},
                timeout=30.0,
            )
            if resp.is_success:
                delivered.append("journal")
        except httpx.HTTPError:
            logger.warning("Journal delivery failed", exc_info=True)
    return delivered


# ---------------------------------------------------------------- scheduler


async def _tick() -> None:
    now = datetime.now(UTC)
    for automation, scheduled_for in await store.claim_due(now):
        if now - scheduled_for > CATCH_UP:
            run_id = await store.start_run(automation["id"], "schedule", status="missed")
            await store.finish_run(run_id, "missed", error=f"was due {scheduled_for.isoformat()}")
            continue
        await trigger_run(automation, "schedule")


async def _loop() -> None:
    while True:
        try:
            await _tick()
        except Exception:
            logger.exception("Automation scheduler tick failed")
        await asyncio.sleep(POLL_SECONDS)


async def start() -> None:
    global _task
    if not settings.automations_scheduler or not settings.database_url:
        logger.info("Automation scheduler disabled")
        return
    # Automations must never stop Sazed from booting: if the tables are missing (e.g. a database
    # whose migration tracker skipped 003), log it and run without the scheduler.
    try:
        await store.mark_stale_runs()
    except Exception:
        logger.exception("Automation scheduler not started: automation tables unavailable")
        return
    _task = asyncio.create_task(_loop())
    logger.info("Automation scheduler started (every %ss)", POLL_SECONDS)


async def stop() -> None:
    if _task is not None:
        _task.cancel()
