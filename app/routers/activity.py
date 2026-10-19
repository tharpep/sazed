"""Activity API for the console: feed, one event, a flow, a summary, and a live SSE stream."""

import asyncio
import json
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.activity import live, service

router = APIRouter()

HEARTBEAT_SECONDS = 20


@router.get("")
async def list_events(
    categories: str | None = Query(None, description="Comma-separated: chat,llm,tool,..."),
    errors: bool = Query(False),
    before_id: int | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
):
    cats = [c for c in (categories or "").split(",") if c]
    return {"events": await service.list_events(cats, errors, before_id, None, limit)}


@router.get("/summary")
async def summary():
    return await service.summary()


@router.get("/events/{event_id}")
async def get_event(event_id: int):
    event = await service.get_event(event_id)
    if event is None:
        raise HTTPException(404, "Event not found")
    return event


@router.get("/public-map")
async def public_map_snapshot():
    """The snapshot the portfolio gets (what app/activity/public_map.py pushes each minute)."""
    from app.activity import public_map

    return await public_map.build_snapshot()


@router.get("/flow")
async def flow(trace: str = Query(..., min_length=3, max_length=200)):
    return await service.flow(trace)


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


@router.get("/stream")
async def stream(request: Request, after_id: int | None = Query(None)):
    """Server-sent events: `activity` (new timeline rows), `step` (job steps), and a comment
    heartbeat. With after_id, rows the page missed while disconnected come first."""
    q = live.subscribe()

    async def events():
        try:
            yield _sse("ready", {})
            if after_id is not None:
                missed = await service.list_events(None, False, None, after_id, 500)
                for row in reversed(missed):
                    yield _sse("activity", row)
            while True:
                if await request.is_disconnected() or not live.is_subscribed(q):
                    break
                try:
                    kind, data = await asyncio.wait_for(q.get(), timeout=HEARTBEAT_SECONDS)
                except TimeoutError:
                    yield ": ping\n\n"
                    continue
                yield _sse(kind, data)
        finally:
            live.unsubscribe(q)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
