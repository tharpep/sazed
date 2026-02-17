"""Session processing pipeline — fact extraction and summarization."""

import asyncio
import json
from typing import Any

import anthropic

from app.agent.memory import load_memory, upsert_fact
from app.config import settings

_client: anthropic.AsyncAnthropic | None = None


def _get_client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    return _client


def _format_messages(messages: list[dict[str, Any]]) -> str:
    """
    Convert the messages array to readable text for the LLM.
    Includes user text and assistant text. Briefly notes tool calls.
    Skips raw tool results (too noisy for extraction/summarization).
    """
    lines = []
    for msg in messages:
        role = msg["role"].upper()
        content = msg["content"]

        if isinstance(content, str):
            lines.append(f"{role}: {content}")
            continue

        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text":
                    lines.append(f"{role}: {block['text']}")
                elif btype == "tool_use":
                    lines.append(f"{role} [called {block['name']}]")
                # tool_result blocks skipped intentionally

    return "\n\n".join(lines)


def _format_existing_facts(facts: list[dict[str, Any]]) -> str:
    if not facts:
        return "(none)"
    return "\n".join(f"- [{f['fact_type']}] {f['key']}: {f['value']}" for f in facts)


def _parse_json_list(text: str) -> list[dict[str, Any]]:
    """Parse a JSON array from LLM output, handling markdown code fences."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1])
    try:
        result = json.loads(text)
        return result if isinstance(result, list) else []
    except json.JSONDecodeError:
        return []


async def _extract_facts(
    messages: list[dict[str, Any]],
    existing_facts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Ask Haiku to extract personal facts from the conversation."""
    conversation = _format_messages(messages)
    existing = _format_existing_facts(existing_facts)

    prompt = f"""Extract personal facts about the user from this conversation.
Only extract facts that are explicitly stated or clearly implied.
Do not duplicate facts already in the existing list unless the value has changed.

Return a JSON array of objects with these fields:
  fact_type: one of "personal", "preference", "project", "instruction", "relationship"
  key: short identifier, e.g. "primary_language"
  value: the fact value, e.g. "Python"
  confidence: 1.0 if explicitly stated, 0.7 if clearly implied

Return [] if no new or updated facts are found.
Return only the JSON array, no other text.

Existing facts:
{existing}

Conversation:
{conversation}"""

    response = await _get_client().messages.create(
        model=settings.haiku_model,
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    return _parse_json_list(response.content[0].text)


async def _summarize(messages: list[dict[str, Any]]) -> str:
    """Ask Haiku to summarize the session."""
    conversation = _format_messages(messages)

    prompt = f"""Summarize this conversation in 1-3 paragraphs.
Focus on: key topics discussed, decisions made, action items, and important information shared.
Be concise and factual.

Conversation:
{conversation}"""

    response = await _get_client().messages.create(
        model=settings.haiku_model,
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text.strip()


async def process_session(
    session_id: str,
    messages: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    Run fact extraction and summarization in parallel.
    Upserts extracted facts to agent_memory.
    Summary is stored locally for now — POST to /kb/ingest once MY-AI is deployed.
    """
    if not messages:
        return {"session_id": session_id, "facts_extracted": 0, "summary": ""}

    existing_facts = await load_memory()

    # Run both Haiku calls in parallel
    raw_facts, summary = await asyncio.gather(
        _extract_facts(messages, existing_facts),
        _summarize(messages),
    )

    # Upsert extracted facts — only overwrites if confidence >= existing
    upserted = []
    for fact in raw_facts:
        try:
            result = await upsert_fact(
                fact_type=fact["fact_type"],
                key=fact["key"],
                value=fact["value"],
                confidence=float(fact.get("confidence", 0.7)),
                source=session_id,
            )
            upserted.append(result)
        except (KeyError, ValueError):
            continue

    # TODO (Phase 3.5 + MY-AI): POST summary to gateway /kb/ingest
    # with source_category="conversations" and metadata={session_id, date}

    return {
        "session_id": session_id,
        "facts_extracted": len(upserted),
        "summary": summary,
    }
