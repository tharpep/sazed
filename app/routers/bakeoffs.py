"""Bake-offs (My-AI plans/14): one change task run by 2-3 Claude models, compared, one promoted."""

import re
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.jobs import service

router = APIRouter()

_ID = re.compile(r"^[0-9a-f]{8}$")


def _bakeoff_id(value: str) -> str:
    if not _ID.match(value):
        raise HTTPException(404, "Bake-off not found")
    return value


class BakeoffRequest(BaseModel):
    repo: str = Field(..., min_length=1, max_length=200)
    instruction: str = Field(..., min_length=3, max_length=8000)
    models: list[str] = Field(..., min_length=2, max_length=3)
    requested_by: Literal["console", "mcp"] = "console"


class PromoteRequest(BaseModel):
    arm: str = Field(..., min_length=1, max_length=40)


@router.post("", status_code=201)
async def start_bakeoff(body: BakeoffRequest):
    try:
        return await service.create_bakeoff(
            body.repo, body.instruction, body.models, body.requested_by
        )
    except service.JobError as e:
        raise HTTPException(422, str(e)) from e


@router.get("/{bakeoff_id}")
async def get_bakeoff(bakeoff_id: str):
    try:
        return await service.bakeoff_report(_bakeoff_id(bakeoff_id))
    except service.JobError as e:
        raise HTTPException(404, str(e)) from e


@router.post("/{bakeoff_id}/promote")
async def promote(bakeoff_id: str, body: PromoteRequest):
    try:
        return await service.promote_bakeoff(_bakeoff_id(bakeoff_id), body.arm.strip())
    except service.JobError as e:
        raise HTTPException(422, str(e)) from e
