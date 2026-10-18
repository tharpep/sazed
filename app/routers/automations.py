"""Automations REST API — used by the console page; Sazed's chat tools call the same store."""

import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import ValidationError

from app.automations import runner, store
from app.automations.models import (
    AutomationCreate,
    AutomationUpdate,
    CronSchedule,
    SchedulePreview,
)

router = APIRouter()


def _upcoming(trigger: dict, enabled: bool) -> list[datetime]:
    if not enabled:
        return []
    return CronSchedule(**trigger).upcoming(datetime.now(UTC), 3)


def _with_upcoming(row: dict[str, Any]) -> dict[str, Any]:
    return {**row, "upcoming": _upcoming(row["trigger"], row["enabled"])}


async def _get_or_404(automation_id: uuid.UUID) -> dict[str, Any]:
    row = await store.get_automation(automation_id)
    if row is None:
        raise HTTPException(404, "Automation not found")
    return row


@router.get("")
async def list_automations():
    """All automations with their last run status, enabled first, soonest next run first."""
    return [_with_upcoming(r) for r in await store.list_automations()]


@router.post("", status_code=201)
async def create_automation(body: AutomationCreate):
    return _with_upcoming(await store.create_automation(body))


@router.post("/preview-schedule")
async def preview_schedule(body: SchedulePreview):
    """Validate a cron expression and show the next five run times (for the console form)."""
    try:
        schedule = CronSchedule(cron=body.cron, timezone=body.timezone)
    except ValidationError as e:
        raise HTTPException(422, e.errors()[0]["msg"]) from e
    return {"upcoming": schedule.upcoming(datetime.now(UTC), 5)}


@router.get("/{automation_id}")
async def get_automation(automation_id: uuid.UUID, run_limit: int = Query(20, ge=1, le=100)):
    row = await _get_or_404(automation_id)
    return {**_with_upcoming(row), "runs": await store.list_runs(automation_id, run_limit)}


@router.patch("/{automation_id}")
async def update_automation(automation_id: uuid.UUID, body: AutomationUpdate):
    row = await store.update_automation(automation_id, body)
    if row is None:
        raise HTTPException(404, "Automation not found")
    return _with_upcoming(row)


@router.delete("/{automation_id}", status_code=204)
async def delete_automation(automation_id: uuid.UUID):
    if not await store.delete_automation(automation_id):
        raise HTTPException(404, "Automation not found")


@router.post("/{automation_id}/run", status_code=202)
async def run_now(automation_id: uuid.UUID):
    """Start a run in the background; poll GET /automations/{id} for its result."""
    row = await _get_or_404(automation_id)
    run_id = await runner.trigger_run(row, "manual")
    if run_id is None:
        raise HTTPException(409, "A run is already in progress")
    return {"run_id": run_id}
