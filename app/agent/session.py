"""Session processing pipeline — fact extraction and summarization."""

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

import anthropic
import httpx

from app.agent.memory import load_memory, upsert_fact
from app.config import settings

logger = logging.getLogger(__name__)

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


async def compress_context(
    overflow_messages: list[dict[str, Any]],
    existing_summary: str | None,
) -> str:
    """Compress overflow messages into a rolling context summary for the session."""
    conversation = _format_messages(overflow_messages)

    if existing_summary:
        prompt = f"""Update the following conversation summary to incorporate new messages.

Existing summary:
{existing_summary}

New messages to incorporate:
{conversation}

Produce a single updated summary covering everything. Be concise — this will be prepended to future messages as background context."""
    else:
        prompt = f"""Summarize the following conversation as background context for a personal AI assistant.
Focus on key decisions, important information exchanged, and action items.
Be concise — this summary will be prepended to future messages to maintain context.

Conversation:
{conversation}"""

    response = await _get_client().messages.create(
        model=settings.haiku_model,
        max_tokens=768,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text.strip()


async def _extract_facts(
    messages: list[dict[str, Any]],
    existing_facts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Ask Haiku to extract personal facts from the conversation."""
    conversation = _format_messages(messages)
    existing = _format_existing_facts(existing_facts)

    prompt = f"""You are building a persistent memory for a personal AI assistant.
These facts will be injected into every future conversation so the assistant can be more helpful and personalized over time. Extract only facts that are durable and personally meaningful — things that will still be relevant weeks or months from now.

Extract facts in these categories:
- Personal info: name, location, occupation, school, timezone
- Stable preferences: tools, languages, formats, communication style
- Ongoing projects and long-term goals
- Standing relationships and regular collaborators
- Explicit instructions the user wants the assistant to always follow

Do NOT extract:
- Transient details: current mood, today's plans, one-time requests, deadlines that will pass
- Facts that only make sense within this specific conversation
- Anything not explicitly stated or clearly implied by the user

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


async def _generate_kb_summary(messages: list[dict[str, Any]], session_dt: datetime) -> str:
    """Generate a rich, structured KB summary for Drive ingestion."""
    conversation = _format_messages(messages)

    prompt = f"""Generate a structured knowledge base entry for this conversation session.
Include only sections that have meaningful content:

**Topics:** comma-separated list of main topics discussed
**Summary:** 2-4 sentences covering what was discussed
**Decisions:** bullet list of concrete decisions or conclusions reached
**Follow-ups:** bullet list of action items or things to revisit
**Entities:** comma-separated list of files, projects, people, or tools specifically referenced

Be specific and factual. Include enough detail that this entry is useful without the full conversation.

Conversation:
{conversation}"""

    response = await _get_client().messages.create(
        model=settings.haiku_model,
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )
    body = response.content[0].text.strip()
    date_str = session_dt.strftime("%B %d, %Y at %I:%M %p UTC")
    return f"# Session — {date_str}\n\n{body}"


async def _ingest_session_to_kb(summary: str, session_dt: datetime) -> None:
    """Write session summary to Drive and trigger KB sync."""
    if not settings.conversations_folder_id:
        logger.warning("conversations_folder_id not configured — skipping KB ingestion")
        return

    filename = f"session-{session_dt.strftime('%Y-%m-%d-%H%M%S')}.md"
    base = settings.gateway_url.rstrip("/")
    headers = {"X-API-Key": settings.gateway_api_key} if settings.gateway_api_key else {}

    async with httpx.AsyncClient(timeout=30.0) as client:
        write_resp = await client.post(
            f"{base}/storage/files",
            json={
                "name": filename,
                "content": summary,
                "folder_id": settings.conversations_folder_id,
                "mime_type": "text/plain",
            },
            headers=headers,
        )
        if not write_resp.is_success:
            logger.error(
                f"Failed to write session summary to Drive: "
                f"{write_resp.status_code} {write_resp.text}"
            )
            return

        logger.debug(f"Session summary written to Drive: {filename}")

        sync_resp = await client.post(f"{base}/kb/sync", headers=headers)
        if not sync_resp.is_success:
            logger.warning(f"KB sync trigger failed after session ingestion: {sync_resp.status_code}")
        else:
            logger.debug("KB sync triggered after session ingestion")


async def process_session(
    session_id: str,
    messages: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    Run fact extraction, summarization, and KB ingestion in parallel where enabled.
    Upserts extracted facts to agent_memory.
    When kb_ingest_enabled, writes a structured session summary to Drive and triggers KB sync.
    """
    if not messages:
        return {"session_id": session_id, "facts_extracted": 0, "summary": ""}

    session_dt = datetime.now(timezone.utc)
    logger.debug(f"process_session {session_id}: {len(messages)} messages to process")
    existing_facts = await load_memory()

    # Build coroutine map so all active tasks run in parallel
    coros: dict[str, Any] = {"facts": _extract_facts(messages, existing_facts)}
    if settings.session_summarization:
        coros["summary"] = _summarize(messages)
    if settings.conversations_folder_id:
        coros["kb_summary"] = _generate_kb_summary(messages, session_dt)

    results = dict(zip(coros.keys(), await asyncio.gather(*coros.values())))

    raw_facts = results["facts"]
    summary = results.get("summary", "")
    kb_summary = results.get("kb_summary", "")

    logger.debug(
        f"process_session {session_id}: extracted {len(raw_facts)} raw fact(s), "
        f"summarization={'on' if settings.session_summarization else 'off'}"
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

    if kb_summary:
        try:
            await _ingest_session_to_kb(kb_summary, session_dt)
        except Exception as e:
            logger.error(f"KB ingestion failed for session {session_id}: {e}")

    return {
        "session_id": session_id,
        "facts_extracted": len(upserted),
        "summary": summary,
    }
