"""The notes filer: one cheap model call decides a topic for one note."""

import json
import re

from app.agent.client import get_client, model_params, schedule_llm_log
from app.agent.json_utils import strip_json_fence
from app.config import settings
from app.notes.filing import KINDS, FilerError, FilerUsage, Filing
from app.notes.prompt import SYSTEM
from app.pricing import estimate_cost_usd

_TAG = re.compile(r"(?<![\w/#&])#([A-Za-z][A-Za-z0-9_-]*)")
_TOPIC = re.compile(r"[A-Za-z0-9][A-Za-z0-9 &'-]{0,39}")
_NOTE_CHARS = 2000
_REASON_CHARS = 200


def _squash(name: str) -> str:
    return re.sub(r"[\s_-]+", "", name).casefold()


def tag_topic(text: str, topics: dict[str, int]) -> tuple[str, str] | None:
    match = _TAG.search(text)
    if match is None:
        return None
    tag = match.group(1)
    for name in topics:
        if _squash(name) == _squash(tag):
            return name, tag
    spaced = re.sub(r"[-_]", " ", tag)
    return spaced[0].upper() + spaced[1:], tag


def messages_for(text: str, topics: dict[str, int]) -> list[dict]:
    listed = ", ".join(f"{name} ({count})" for name, count in topics.items()) or "(none yet)"
    content = (
        f"Existing topics, with how many notes each holds: {listed}\n\n"
        f"<note>\n{text[:_NOTE_CHARS]}\n</note>"
    )
    return [{"role": "user", "content": content}]


def parse_filing(answer: str, topics: dict[str, int]) -> Filing:
    try:
        data = json.loads(strip_json_fence(answer))
    except ValueError as exc:
        raise FilerError("answer is not JSON") from exc
    if not isinstance(data, dict):
        raise FilerError("answer is not a JSON object")
    topic = data.get("topic")
    if not isinstance(topic, str) or not _TOPIC.fullmatch(topic.strip()):
        raise FilerError("topic is missing or malformed")
    topic = topic.strip()
    existing = next((name for name in topics if name.casefold() == topic.casefold()), None)
    reason = data.get("reason")
    if not isinstance(reason, str):
        raise FilerError("reason is not a string")
    reason = " ".join(reason.split())[:_REASON_CHARS].strip()
    if not reason:
        raise FilerError("reason is empty")
    kind = data.get("kind")
    kind = kind.lower() if isinstance(kind, str) else None
    return Filing(
        topic=existing or topic,
        new_topic=existing is None,
        kind=kind if kind in KINDS else None,
        reason=reason,
        by_tag=False,
    )


async def file_text(text: str, topics: dict[str, int]) -> tuple[Filing, FilerUsage | None]:
    text = text.strip()
    if not text:
        raise ValueError("text is empty")
    tagged = tag_topic(text, topics)
    if tagged is not None:
        topic, tag = tagged
        filing = Filing(
            topic=topic,
            new_topic=topic not in topics,
            kind=None,
            reason=f"Because you typed #{tag}.",
            by_tag=True,
        )
        return filing, None
    resp = await get_client().messages.create(
        model=settings.haiku_model,
        **model_params(settings.haiku_model),
        max_tokens=300,
        system=SYSTEM,
        messages=messages_for(text, topics),
    )
    if settings.llm_cost_tracking:
        schedule_llm_log(None, None, settings.haiku_model, "note_file", resp)
    usage = getattr(resp, "usage", None)
    input_tokens = getattr(usage, "input_tokens", 0) or 0
    output_tokens = getattr(usage, "output_tokens", 0) or 0
    cost = estimate_cost_usd(settings.haiku_model, input_tokens, output_tokens)
    answer = next((block.text for block in resp.content if block.type == "text"), None)
    if answer is None:
        raise FilerError("the model returned no text")
    return parse_filing(answer, topics), FilerUsage(input_tokens, output_tokens, cost)
