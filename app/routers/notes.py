import anthropic
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.notes.filer import file_text
from app.notes.filing import FilerError

router = APIRouter()


class TryBody(BaseModel):
    text: str = Field(min_length=1, max_length=100_000)
    topics: dict[str, int] = Field(default_factory=dict, max_length=200)


class UsageOut(BaseModel):
    input_tokens: int
    output_tokens: int
    cost_usd: float | None


class TryOut(BaseModel):
    topic: str
    new_topic: bool
    kind: str | None
    reason: str
    by_tag: bool
    filer: str
    usage: UsageOut | None


@router.post("/filer/try", response_model=TryOut)
async def try_filer(body: TryBody) -> TryOut:
    """Dry run: files one text and never reads or writes a note."""
    if not body.text.strip():
        raise HTTPException(400, "text is empty")
    try:
        filing, usage = await file_text(body.text, body.topics)
    except FilerError as exc:
        raise HTTPException(502, f"The filer's answer could not be read: {exc}") from exc
    except anthropic.APIError as exc:
        raise HTTPException(502, "The model call failed") from exc
    return TryOut(
        topic=filing.topic,
        new_topic=filing.new_topic,
        kind=filing.kind,
        reason=filing.reason,
        by_tag=filing.by_tag,
        filer=filing.filer,
        usage=(
            UsageOut(
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cost_usd=usage.cost_usd,
            )
            if usage
            else None
        ),
    )
