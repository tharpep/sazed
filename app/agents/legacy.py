"""Triage and the map check on the runner as one step each, until phase 2 ports them."""

import uuid
from typing import Any

from pydantic import BaseModel

from app.agents.steps import AgentSpec, Outcome, Step


class LegacyContext(BaseModel):
    run_id: str
    outcome: tuple[str, str, str | None, str | None] | None = None


async def _triage(ctx: LegacyContext) -> dict[str, Any]:
    from app.backlog import pipelines

    ctx.outcome = await pipelines.triage(uuid.UUID(ctx.run_id))
    return {"detail": ctx.outcome[0]}


async def _map(ctx: LegacyContext) -> dict[str, Any]:
    from app.activity import map_report

    ctx.outcome = await map_report.run(uuid.UUID(ctx.run_id))
    return {"detail": ctx.outcome[0]}


def _summarize(ctx: LegacyContext) -> Outcome:
    status, text, notify_text, notify_url = ctx.outcome
    return Outcome(status, text, notify_text, notify_url)


TRIAGE = AgentSpec("triage", LegacyContext, (Step("triage", "code", _triage),), _summarize)
MAP = AgentSpec("map", LegacyContext, (Step("map check", "code", _map),), _summarize)
