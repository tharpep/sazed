"""Session processing pipeline — fact extraction and summarization."""

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from app.agent.client import get_client, schedule_llm_log
from app.agent.json_utils import strip_json_fence
from app.agent.memory import load_memory, upsert_fact
from app.agent.procedures import list_procedures, propose_procedure_from_session
from app.config import settings
from app.db import get_pool
from app.http_client import get_client as get_http_client

logger = logging.getLogger(__name__)


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


def _notable_action_logs(action_logs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rows worth reflecting on: real failures, plus any row belonging to a
    (tool_name, input) pair that was actually executed 2+ times this session.

    The latter is the strongest available proxy for a stuck loop: loop.py's
    stuck-loop detector breaks *before* logging the 3rd (blocking) call with an
    identical signature, so the persisted evidence of thrash is two prior
    successful executions of the same call, not a 3rd failed one.
    """
    sig_counts: dict[tuple[str, str], int] = {}
    for r in action_logs:
        raw_input = r["input"]
        sig = (r["tool_name"], raw_input if isinstance(raw_input, str) else json.dumps(raw_input))
        sig_counts[sig] = sig_counts.get(sig, 0) + 1

    notable = []
    for r in action_logs:
        raw_input = r["input"]
        sig = (r["tool_name"], raw_input if isinstance(raw_input, str) else json.dumps(raw_input))
        if r["status"] == "error" or sig_counts[sig] >= 2:
            notable.append(r)
    return notable


def _format_notable_action_logs(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "(none)"
    lines = []
    for r in rows:
        raw_input = r["input"]
        try:
            args = json.loads(raw_input) if isinstance(raw_input, str) else raw_input
        except (json.JSONDecodeError, TypeError):
            args = raw_input
        err = f" — {r['error_message']}" if r.get("error_message") else ""
        lines.append(f"- {r['tool_name']}({args}) → {r['status']}{err}")
    return "\n".join(lines)


def _parse_json_list(text: str) -> list[dict[str, Any]]:
    """Parse a JSON array from LLM output, handling markdown code fences."""
    try:
        result = json.loads(strip_json_fence(text))
        return result if isinstance(result, list) else []
    except json.JSONDecodeError:
        return []


async def compress_context(
    overflow_messages: list[dict[str, Any]],
    existing_summary: str | None,
    session_id: uuid.UUID | str | None = None,
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

    response = await get_client().messages.create(
        model=settings.haiku_model,
        max_tokens=768,
        messages=[{"role": "user", "content": prompt}],
    )
    if settings.llm_cost_tracking:
        schedule_llm_log(session_id, None, settings.haiku_model, "context_compress", response)
    return response.content[0].text.strip()


async def _extract_facts(
    messages: list[dict[str, Any]],
    existing_facts: list[dict[str, Any]],
    session_id: str | None = None,
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
  key: short snake_case identifier — use the same key as an existing fact if it refers to the same concept, e.g. "primary_language" not "main_language" or "preferred_language"
  value: the fact value, e.g. "Python"
  confidence: 1.0 if explicitly stated, 0.7 if clearly implied

Return [] if no new or updated facts are found.
Return only the JSON array, no other text.

Existing facts:
{existing}

Conversation:
{conversation}"""

    response = await get_client().messages.create(
        model=settings.haiku_model,
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    if settings.llm_cost_tracking:
        schedule_llm_log(session_id, None, settings.haiku_model, "facts", response)
    return _parse_json_list(response.content[0].text)


async def _summarize(messages: list[dict[str, Any]], session_id: str | None = None) -> str:
    """Ask Haiku to summarize the session."""
    conversation = _format_messages(messages)

    prompt = f"""Summarize this conversation in 1-3 paragraphs.
Focus on: key topics discussed, decisions made, action items, and important information shared.
Be concise and factual.

Conversation:
{conversation}"""

    response = await get_client().messages.create(
        model=settings.haiku_model,
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )
    if settings.llm_cost_tracking:
        schedule_llm_log(session_id, None, settings.haiku_model, "summary", response)
    return response.content[0].text.strip()


async def _generate_kb_summary(
    messages: list[dict[str, Any]],
    session_dt: datetime,
    message_count: int | None = None,
    session_start: datetime | None = None,
    session_id: str | None = None,
) -> str:
    """Generate a rich, structured KB summary for Drive ingestion."""
    conversation = _format_messages(messages)
    count = message_count if message_count is not None else len(messages)

    prompt = f"""Generate a structured knowledge base entry for this conversation session.
Include only sections that have meaningful content:

**Topics:** comma-separated list of main topics discussed
**Summary:** 2-4 sentences covering what was discussed
**Actions:** bullet list of things Sazed actually did (files read, searches run, tool calls made, data fetched)
**Decisions:** bullet list of concrete decisions or conclusions reached
**Follow-ups:** bullet list of action items or things to revisit
**Entities:** comma-separated list of files, projects, people, or tools specifically referenced

Be specific and factual. Include enough detail that this entry is useful without the full conversation.

Conversation:
{conversation}"""

    response = await get_client().messages.create(
        model=settings.haiku_model,
        max_tokens=768,
        messages=[{"role": "user", "content": prompt}],
    )
    if settings.llm_cost_tracking:
        schedule_llm_log(session_id, None, settings.haiku_model, "kb_summary", response)
    body = response.content[0].text.strip()
    date_str = session_dt.strftime("%B %d, %Y at %I:%M %p UTC")

    meta_parts = [f"{count} messages"]
    if session_start:
        duration_mins = int((session_dt - session_start).total_seconds() / 60)
        if duration_mins < 60:
            meta_parts.append(f"~{duration_mins} min")
        else:
            hours, mins = divmod(duration_mins, 60)
            meta_parts.append(f"~{hours}h {mins}m")
    meta = " · ".join(meta_parts)

    return f"# Session — {date_str}\n*{meta}*\n\n{body}"


_MAX_ACTION_LOGS_IN_PROMPT = 20  # cap prompt size for unusually chatty/failing sessions


async def _reflect(
    session_id: str,
    conversation_text: str,
    notable_logs: list[dict[str, Any]],
    existing_instructions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Haiku call: turn a session's failures/thrash into 0-N behavioral lessons.

    Only ever writes lessons about Sazed's OWN tool-calling behavior — never
    about the user (that's fact-extraction's job). Caller guarantees
    notable_logs is non-empty (checked before this coroutine is scheduled) so
    every invocation of this function makes exactly one Haiku call.
    """
    existing = _format_existing_facts(existing_instructions)
    # notable_logs is chronological (ORDER BY timestamp) — keep the most recent
    # ones, since what Sazed should do differently "next time" is best judged
    # from what just happened, not the earliest failures in a long session.
    logs_preview = _format_notable_action_logs(notable_logs[-_MAX_ACTION_LOGS_IN_PROMPT:])

    prompt = f"""You are reviewing a session where Sazed (a personal AI assistant) hit tool
failures or got stuck repeating the same tool call. Extract generalizable lessons about
Sazed's OWN tool-calling behavior — never about the user, and never fabricated from a
single isolated failure that looks like a transient error (e.g. one unexplained 5xx with
no retry). Only propose a lesson if the pattern suggests something Sazed should genuinely
do differently next time: wrong tool order, a missing prerequisite call, malformed args,
or retrying the same broken approach without adapting.

Do not duplicate an existing instruction — if a lesson already covers this, skip it.
Existing instructions:
{existing}

Failed/repeated tool calls this session, in order:
{logs_preview}

Conversation:
{conversation_text}

Return a JSON array of 0 to {settings.max_lessons_per_session} objects, each:
  scope: "instruction" (a behavioral rule) or "procedure" (a better tool ordering/recipe)
  key: short snake_case identifier — reuse an existing instruction's key if this refines it
  lesson: one concise sentence Sazed should follow next time

Return [] if nothing genuinely generalizable happened. Return only the JSON array, no other text."""

    response = await get_client().messages.create(
        model=settings.haiku_model,
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )
    if settings.llm_cost_tracking:
        schedule_llm_log(session_id, None, settings.haiku_model, "reflection", response)

    raw_lessons = _parse_json_list(response.content[0].text)[: settings.max_lessons_per_session]

    applied: list[dict[str, Any]] = []
    for item in raw_lessons:
        try:
            scope = item.get("scope", "instruction")
            key = item["key"]
            lesson = item["lesson"]
        except (KeyError, AttributeError):
            continue

        # v1: procedure-scope lessons fall back to an instruction fact too — there's
        # no way to patch an existing procedure's steps yet (app/agent/procedures.py
        # only supports proposing a brand-new one), so there's nowhere else to send
        # a procedure-scope correction. `scope` is kept in the prompt/output as a
        # forward-looking hook for whenever that patch capability gets built.
        await upsert_fact(
            fact_type="instruction",
            key=key,
            value=lesson,
            confidence=0.8,
            source=f"reflection:{session_id}",
        )
        applied.append({"scope": scope, "key": key, "lesson": lesson})

    if applied:
        logger.info(f"process_session {session_id}: reflection produced {len(applied)} lesson(s)")
    return applied


async def _ingest_session_to_kb(summary: str, session_dt: datetime) -> tuple[bool, str]:
    """Directly ingest the session summary into the KB. Returns (success, error_message).

    Replaces the old Drive-write-then-sync round-trip (#101) — no synthetic
    Drive file, no full-corpus sync trigger, embeds immediately via the KB
    service's direct-ingest endpoint added in #100.
    """
    title = f"session-{session_dt.strftime('%Y-%m-%d-%H%M%S')}"
    base = settings.gateway_url.rstrip("/")
    headers = {"X-API-Key": settings.gateway_api_key} if settings.gateway_api_key else {}

    resp = await get_http_client().post(
        f"{base}/kb/ingest/text",
        json={"title": title, "content": summary},
        headers=headers,
        timeout=30.0,
    )
    if not resp.is_success:
        msg = f"KB ingest failed ({resp.status_code}): {resp.text}"
        logger.error(f"Failed to ingest session summary into KB: {msg}")
        return False, msg

    logger.debug(f"Session summary ingested into KB: {title}")
    return True, ""


async def process_session(
    session_id: str,
    messages: list[dict[str, Any]],
    session_dt: datetime | None = None,
    session_start: datetime | None = None,
    session_type: str = "chat",
) -> dict[str, Any]:
    """
    Run fact extraction, summarization, and KB ingestion in parallel where enabled.
    Upserts extracted facts to agent_memory.
    When session_kb_ingest_enabled, writes a structured session summary directly into the KB.
    session_dt: last_activity timestamp used for the KB entry title; defaults to now if not provided.
    session_start: created_at timestamp used to compute session duration; omitted if unavailable.
    session_type: only "chat" sessions run fact extraction and KB ingestion — other session
    types are compress-only (summarization still runs if enabled) to avoid polluting memory
    and the KB with non-conversational traffic.
    """
    if not messages:
        return {"session_id": session_id, "facts_extracted": 0, "summary": ""}

    session_dt = session_dt or datetime.now(timezone.utc)
    message_count = len(messages)
    logger.debug(f"process_session {session_id}: {message_count} messages to process")
    existing_facts = await load_memory()

    action_logs: list[dict[str, Any]] = []
    propose_procedure = False
    run_reflection = False
    notable_logs: list[dict[str, Any]] = []
    if settings.procedural_memory_enabled or settings.reflection_enabled:
        pool = get_pool()
        rows = await pool.fetch(
            "SELECT tool_name, input, status, error_message FROM action_logs "
            "WHERE session_id = $1 ORDER BY timestamp",
            uuid.UUID(session_id),
        )
        action_logs = [dict(row) for row in rows]

        if settings.procedural_memory_enabled:
            write_tool_calls = sum(
                1 for r in action_logs if r["tool_name"] in settings.sonnet_write_tools
            )
            session_failed = any(r["status"] == "error" for r in action_logs)
            propose_procedure = (
                not session_failed and write_tool_calls >= settings.procedure_min_write_tools
            )

        if settings.reflection_enabled and action_logs:
            notable_logs = _notable_action_logs(action_logs)
            run_reflection = bool(notable_logs)

    # Build coroutine map so all active tasks run in parallel.
    # Fact extraction and KB ingestion only run for chat sessions — think/automation
    # traffic is compress-only, so it doesn't pollute memory or the KB.
    coros: dict[str, Any] = {}
    if session_type == "chat":
        coros["facts"] = _extract_facts(messages, existing_facts, session_id=session_id)
    if settings.session_summarization:
        coros["summary"] = _summarize(messages, session_id=session_id)
    if session_type == "chat" and settings.session_kb_ingest_enabled:
        coros["kb_summary"] = _generate_kb_summary(
            messages, session_dt, message_count=message_count, session_start=session_start,
            session_id=session_id,
        )
    if propose_procedure:
        existing_names = [p["name"] for p in await list_procedures(status="active")]
        coros["procedure"] = propose_procedure_from_session(
            session_id, _format_messages(messages), action_logs, existing_names
        )
    if run_reflection:
        instruction_facts = [f for f in existing_facts if f["fact_type"] == "instruction"]
        coros["reflection"] = _reflect(
            session_id, _format_messages(messages), notable_logs, instruction_facts
        )

    results = dict(zip(coros.keys(), await asyncio.gather(*coros.values())))

    raw_facts = results.get("facts", [])
    summary = results.get("summary", "")
    kb_summary = results.get("kb_summary", "")
    proposed_procedure = results.get("procedure")
    lessons_learned = results.get("reflection", [])

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

    kb_ok = False
    kb_error = ""
    if kb_summary:
        try:
            kb_ok, kb_error = await _ingest_session_to_kb(kb_summary, session_dt)
        except Exception as e:
            kb_error = str(e)
            logger.error(f"KB ingestion failed for session {session_id}: {e}")

    return {
        "session_id": session_id,
        "facts_extracted": len(upserted),
        "summary": summary,
        "kb_ingested": kb_ok,
        "kb_error": kb_error,
        "procedure_proposed": proposed_procedure["name"] if proposed_procedure else None,
        "lessons_learned": lessons_learned,
    }
