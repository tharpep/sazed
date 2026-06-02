"""Think endpoint — autonomous proactive reasoning."""

import logging

from fastapi import APIRouter
from pydantic import BaseModel

from app.agent.think_loop import run_think

router = APIRouter()
logger = logging.getLogger(__name__)


class ThinkRequest(BaseModel):
    session_id: str | None = None
    context: str | None = None    # "morning" | "midday" | "evening" | None
    trigger: str | None = None    # optional description of what triggered this
    timezone: str | None = None


class ThinkResponse(BaseModel):
    session_id: str
    acted: bool
    summary: str


@router.post("", response_model=ThinkResponse)
async def think(body: ThinkRequest):
    logger.debug(f"POST /think: context={body.context}, trigger={body.trigger}")
    session_id, acted, summary = await run_think(
        body.session_id, body.context, body.trigger, body.timezone
    )
    logger.debug(f"POST /think: done, acted={acted}, summary='{summary[:120]}'")
    return ThinkResponse(session_id=session_id, acted=acted, summary=summary)
