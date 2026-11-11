import uuid
from typing import Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from app.db import get_pool

router = APIRouter()

MS_COLUMNS = (
    "utterance_ms",
    "endpoint_ms",
    "stt_ms",
    "setup_ms",
    "llm_ttft_ms",
    "first_text_ms",
    "tts_first_byte_ms",
    "tts_first_sound_ms",
    "first_sound_ms",
)

Ms = Field(default=None, ge=0)


class VoiceTurn(BaseModel):
    session_id: uuid.UUID | None = None
    kiosk: bool = False
    outcome: Literal["spoken", "silent", "interrupted", "stopped", "error"]
    tool_turn: bool = False
    engine: str | None = None
    utterance_ms: int | None = Ms
    endpoint_ms: int | None = Ms
    stt_ms: int | None = Ms
    setup_ms: int | None = Ms
    llm_ttft_ms: int | None = Ms
    first_text_ms: int | None = Ms
    tts_first_byte_ms: int | None = Ms
    tts_first_sound_ms: int | None = Ms
    first_sound_ms: int | None = Ms


_INSERT_COLUMNS = ("session_id", "kiosk", "outcome", "tool_turn", "engine", *MS_COLUMNS)


@router.post("/turns", status_code=201)
async def create_turn(body: VoiceTurn) -> dict:
    values = body.model_dump()
    placeholders = ", ".join(f"${i}" for i in range(1, len(_INSERT_COLUMNS) + 1))
    row = await get_pool().fetchrow(
        f"INSERT INTO voice_turns ({', '.join(_INSERT_COLUMNS)}) "
        f"VALUES ({placeholders}) RETURNING id",
        *(values[c] for c in _INSERT_COLUMNS),
    )
    return {"id": str(row["id"])}


@router.get("/turns/summary")
async def turns_summary(
    days: int = Query(7, ge=1, le=365), kiosk: bool | None = None
) -> dict:
    args: list = [days]
    where = "created_at > NOW() - make_interval(days => $1)"
    if kiosk is not None:
        args.append(kiosk)
        where += " AND kiosk = $2"
    # percentile_cont skips NULLs, so each slice only counts turns that reached its stage.
    slices = ", ".join(
        f"count({c}) AS {c}_n, "
        f"percentile_cont(0.5) WITHIN GROUP (ORDER BY {c}) AS {c}_p50, "
        f"percentile_cont(0.9) WITHIN GROUP (ORDER BY {c}) AS {c}_p90"
        for c in MS_COLUMNS
    )
    row = await get_pool().fetchrow(
        f"SELECT count(*) AS turns, {slices} FROM voice_turns WHERE {where}", *args
    )

    def pct(value: float | None) -> int | None:
        return None if value is None else round(value)

    return {
        "days": days,
        "turns": row["turns"],
        "slices": {
            c: {
                "n": row[f"{c}_n"],
                "p50": pct(row[f"{c}_p50"]),
                "p90": pct(row[f"{c}_p90"]),
            }
            for c in MS_COLUMNS
        },
    }
