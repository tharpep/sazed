"""Learning v1 (failure classes and guard proposals): the summary, and a one-off backfill."""

from fastapi import APIRouter

from app.learning import service

router = APIRouter()


@router.get("")
async def summary():
    return await service.summary()


@router.post("/backfill")
async def backfill():
    return {"set": await service.backfill()}
