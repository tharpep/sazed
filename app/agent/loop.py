"""Agent loop — core reasoning cycle."""

import asyncio
import json
import logging
import time
import uuid
from collections import Counter
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from typing import Any, AsyncIterator

import anthropic

from app.agent.memory import format_for_prompt, load_memory, load_relevant_memory
from app.agent.session import compress_context
from app.agent.tools import execute_tool, expand_tools, get_tool_schemas, select_tools
from app.config import settings
from app.db import get_pool

logger = logging.getLogger(__name__)

_client: anthropic.AsyncAnthropic | None = None


def _get_client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    return _client


def _tool_sig(name: str, args: dict) -> tuple:
    """Stable hashable signature for a tool call — used for stuck loop detection."""
    return (name, tuple(sorted((k, str(v)) for k, v in args.items())))


async def _build_system_prompt(mode: str = "chat", user_message: str = "", location=None) -> list[dict[str, Any]]:
    memory_section = format_for_prompt(await load_relevant_memory(user_message))
    location_section = ""
    if location:
        location_section = (
            f"\n\n## User's Current Location\n"
            f"Lat: {location.latitude}, Lng: {location.longitude}\n"
            f"Use these coordinates when calling search_places for 'near me' queries."
        )
    blocks: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": (
                "You are Sazed, a personal AI assistant.\n\n"
                "## Behavior\n"
                "- Be direct and concise. No preamble, no filler.\n"
                "- Always use tools to get real data. Never answer from assumption when a tool can verify.\n"
                "- Match response length to the question — short for simple answers, structured only when it genuinely helps.\n"
                "- When a tool fails, say so clearly and suggest what to try instead.\n"
                "- When the user asks you to remember something, call memory_update immediately.\n"
                "- The user can always see what tools you use, so skip action telegraphing.\n\n"
                "## Tools\n"
                "You have tools for: calendar, tasks, email, Google Drive, GitHub, "
                "Google Sheets, notifications, web search, places, and a personal knowledge base.\n\n"
                "## Tool guidance\n"
                "- Tasks: call get_task_lists first to get valid list IDs before creating, reading, or updating tasks.\n"
                "- Drive files: call list_files to find a file ID before reading, updating, or deleting.\n"
                "- Sheets: call get_spreadsheet_info first to confirm tab names and structure before reading or writing.\n"
                "- Knowledge vs web: search the knowledge base first for anything about the user's personal context, projects, or notes. Use web_search when the knowledge base has nothing useful or the topic requires current information.\n"
                "- Email: use list_emails with filters before fetching full message content.\n"
                "- Places: when the user asks about nearby places or 'near me', use the current location coordinates from the system prompt if available.\n"
                "- Available tools: you only receive tools relevant to the current request. "
                "If you need a capability not shown in your current tools, call `request_tools` "
                "with the relevant category — don't tell the user you can't do something until you've tried expanding first."
            ),
            "cache_control": {"type": "ephemeral"},
        },
        {
            "type": "text",
            "text": f"## Known facts about the user\n{memory_section}{location_section}",
            "cache_control": {"type": "ephemeral"},
        },
    ]
    if mode == "voice":
        blocks.append({
            "type": "text",
            "text": (
                "## Voice mode\n"
                "You are responding to a voice interface. Follow these rules strictly:\n"
                "- Reply in 2-3 natural spoken sentences only. Never more.\n"
                "- No bullet points, numbered lists, headers, or markdown of any kind.\n"
                "- Speak conversationally, as if talking to someone in person.\n"
                "- Give the core answer directly. If a topic needs more depth, cover the key point "
                "and offer to elaborate if they want.\n"
                "- Never open with filler words like 'certainly', 'absolutely', or 'of course'.\n"
                "- After using a tool, summarize the result in plain speech — do not repeat raw data, dates in ISO format, or structured output."
            ),
        })
    return blocks



def _content_to_dicts(content: list) -> list[dict[str, Any]]:
    """Convert SDK content blocks to plain dicts for the messages array."""
    result = []
    for block in content:
        if block.type == "text":
            result.append({"type": "text", "text": block.text})
        elif block.type == "tool_use":
            result.append({
                "type": "tool_use",
                "id": block.id,
                "name": block.name,
                "input": block.input,
            })
    return result


def _extract_text(content: list[dict[str, Any]]) -> str:
    for block in content:
        if block.get("type") == "text":
            return block["text"]
    return "I wasn't able to complete that."


async def _apply_context_window(
    pool, session_id: uuid.UUID, messages: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """
    If messages exceed the window, compress overflow into a rolling summary and
    return [summary_pair] + recent messages. Otherwise returns messages unchanged.
    """
    window = settings.session_window_size
    if len(messages) <= window:
        return messages

    overflow = messages[:-window]
    recent = messages[-window:]

    row = await pool.fetchrow(
        "SELECT context_summary, summarized_through FROM sessions WHERE id = $1", session_id
    )
    existing_summary = row["context_summary"] if row else None
    summarized_through = row["summarized_through"] if row else 0

    if len(overflow) > summarized_through:
        logger.debug(
            f"  context window: compressing {len(overflow)} overflow messages "
            f"(previously {summarized_through})"
        )
        summary = await compress_context(overflow, existing_summary)
        await pool.execute(
            "UPDATE sessions SET context_summary = $1, summarized_through = $2 WHERE id = $3",
            summary, len(overflow), session_id,
        )
    else:
        summary = existing_summary or ""

    if not summary:
        return messages

    summary_pair = [
        {"role": "user", "content": f"[Context from earlier in our conversation]\n{summary}"},
        {"role": "assistant", "content": "Understood."},
    ]
    return summary_pair + recent


async def _save_message(pool, session_id: uuid.UUID, role: str, content: Any) -> None:
    """Persist one message row. Content is JSON-encoded to handle both strings and block lists."""
    await pool.execute(
        "INSERT INTO messages (session_id, role, content) VALUES ($1, $2, $3)",
        session_id, role, json.dumps(content),
    )


async def run_turn(session_id: str | None, user_message: str, mode: str = "chat", timezone: str | None = None, location=None) -> tuple[str, str]:
    """
    Run one user turn through the agent loop.
    Returns (session_id, response_text).
    """
    if not session_id:
        session_id = str(uuid.uuid4())

    pool = get_pool()
    sid = uuid.UUID(session_id)

    await pool.execute(
        "INSERT INTO sessions (id) VALUES ($1) ON CONFLICT DO NOTHING", sid
    )

    rows = await pool.fetch(
        "SELECT role, content FROM messages WHERE session_id = $1 ORDER BY timestamp",
        sid,
    )
    messages: list[dict[str, Any]] = [
        {"role": r["role"], "content": json.loads(r["content"])} for r in rows
    ]
    logger.debug(f"session {session_id}: loaded {len(messages)} prior messages")

    messages = await _apply_context_window(pool, sid, messages)

    try:
        tz = ZoneInfo(timezone) if timezone else ZoneInfo("UTC")
    except ZoneInfoNotFoundError:
        tz = ZoneInfo("UTC")
    now = datetime.now(tz)
    tz_prefix = f"[{now.strftime('%A, %B %d, %Y')} {now.strftime('%I:%M %p')} {tz.key}]\n"
    messages.append({"role": "user", "content": tz_prefix + user_message})
    await _save_message(pool, sid, "user", user_message)
    logger.debug(f"session {session_id}: user message='{user_message[:120]}'")

    client = _get_client()
    final_content: list[dict[str, Any]] = []
    system = await _build_system_prompt(mode, user_message, location)
    tools = select_tools(user_message)
    logger.debug(f"  selected {len(tools)} tools for: '{user_message[:80]}'")

    tool_call_counts: Counter = Counter()
    stuck = False

    for turn in range(settings.agent_max_turns):
        model = settings.haiku_model
        t0 = time.perf_counter()
        logger.debug(f"  turn {turn}: calling {model} with {len(messages)} messages in context")
        response = await client.messages.create(
            model=model,
            system=system,
            messages=messages,
            tools=tools,
            max_tokens=4096,
        )
        logger.debug(
            f"  turn {turn}: stop_reason={response.stop_reason} "
            f"in {time.perf_counter() - t0:.3f}s"
        )

        content_dicts = _content_to_dicts(response.content)
        messages.append({"role": "assistant", "content": content_dicts})
        await _save_message(pool, sid, "assistant", content_dicts)
        final_content = content_dicts

        if response.stop_reason == "end_turn":
            break

        if response.stop_reason == "tool_use":
            tool_blocks = [b for b in response.content if b.type == "tool_use"]
            logger.debug(f"  turn {turn}: tool calls → {[b.name for b in tool_blocks]}")

            # Handle request_tools inline — expand tool set before executing others
            expand_results: dict[str, str] = {}
            regular_blocks = []
            for block in tool_blocks:
                if block.name == "request_tools":
                    categories = block.input.get("categories", [])
                    tools, msg = expand_tools(tools, categories)
                    expand_results[block.id] = msg
                    logger.debug(f"  turn {turn}: request_tools {categories} → {msg[:80]}")
                else:
                    regular_blocks.append(block)

            # Execute remaining tool calls concurrently
            raw_results = await asyncio.gather(*[execute_tool(b.name, b.input) for b in regular_blocks])

            tool_results = [
                {"type": "tool_result", "tool_use_id": bid, "content": msg}
                for bid, msg in expand_results.items()
            ]
            log_coros = []
            for block, tool_result in zip(regular_blocks, raw_results):
                sig = _tool_sig(block.name, block.input)
                tool_call_counts[sig] += 1
                if tool_call_counts[sig] >= 3:
                    logger.warning(f"  stuck loop detected: {block.name} called 3x with same args")
                    stuck_msg = (
                        f"I seem to be stuck — I've called `{block.name}` three times with the "
                        f"same arguments without making progress. Please try rephrasing your "
                        f"request or providing more specific details."
                    )
                    final_content = [{"type": "text", "text": stuck_msg}]
                    await _save_message(pool, sid, "assistant", final_content)
                    stuck = True
                    break

                logger.debug(
                    f"  turn {turn}: {block.name} {tool_result.status} in "
                    f"{tool_result.duration_ms}ms, {len(tool_result.content)} chars"
                )
                log_coros.append(pool.execute(
                    """INSERT INTO action_logs
                           (session_id, tool_name, input, output, status, error_message, duration_ms)
                       VALUES ($1, $2, $3::jsonb, $4, $5, $6, $7)""",
                    sid, block.name, json.dumps(block.input), tool_result.content,
                    tool_result.status, tool_result.error, tool_result.duration_ms,
                ))
                tr: dict[str, Any] = {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": tool_result.content,
                }
                if tool_result.status == "error":
                    tr["is_error"] = True
                tool_results.append(tr)

            if stuck:
                break

            await asyncio.gather(*log_coros)
            messages.append({"role": "user", "content": tool_results})
            await _save_message(pool, sid, "user", tool_results)
        else:
            logger.debug(f"  turn {turn}: unexpected stop_reason, bailing out")
            break

    # If the loop exhausted turns mid-tool-use, final_content has no text.
    # Do one synthesis call (no tools) so the user gets an actual response.
    if not any(b.get("type") == "text" for b in final_content):
        logger.debug("  synthesis: loop ended on tool_use, running final synthesis call")
        synth = await client.messages.create(
            model=settings.haiku_model,
            system=system,
            messages=messages,
            max_tokens=1024,
        )
        content_dicts = _content_to_dicts(synth.content)
        await _save_message(pool, sid, "assistant", content_dicts)
        final_content = content_dicts

    await pool.execute(
        """
        UPDATE sessions
        SET last_activity = NOW(),
            message_count = (SELECT COUNT(*) FROM messages WHERE session_id = $1)
        WHERE id = $1
        """,
        sid,
    )

    response_text = _extract_text(final_content)
    logger.debug(f"session {session_id}: final response='{response_text[:120]}'")
    return session_id, response_text


async def run_turn_stream(
    session_id: str | None, user_message: str, mode: str = "chat", timezone: str | None = None, location=None
) -> AsyncIterator[str]:
    """
    Run one user turn through the agent loop, yielding SSE-formatted strings.

    Events yielded:
        event: session    data: {"session_id": "..."}                              — before first LLM call
        event: tool_start data: {"name": "..."}                                    — before each tool execution
        event: tool_done  data: {"name": "...", "status": "success"|"error", "error": "..."|null}
        event: text_delta data: {"text": "..."}                                    — streaming text tokens
        event: done       data: {}                                                 — after session persisted
    """
    if not session_id:
        session_id = str(uuid.uuid4())

    pool = get_pool()
    sid = uuid.UUID(session_id)

    await pool.execute(
        "INSERT INTO sessions (id) VALUES ($1) ON CONFLICT DO NOTHING", sid
    )

    rows = await pool.fetch(
        "SELECT role, content FROM messages WHERE session_id = $1 ORDER BY timestamp",
        sid,
    )
    messages: list[dict[str, Any]] = [
        {"role": r["role"], "content": json.loads(r["content"])} for r in rows
    ]

    messages = await _apply_context_window(pool, sid, messages)

    try:
        tz = ZoneInfo(timezone) if timezone else ZoneInfo("UTC")
    except ZoneInfoNotFoundError:
        tz = ZoneInfo("UTC")
    now = datetime.now(tz)
    tz_prefix = f"[{now.strftime('%A, %B %d, %Y')} {now.strftime('%I:%M %p')} {tz.key}]\n"
    messages.append({"role": "user", "content": tz_prefix + user_message})
    await _save_message(pool, sid, "user", user_message)
    logger.debug(f"stream session {session_id}: user message='{user_message[:120]}'")

    yield f"event: session\ndata: {json.dumps({'session_id': session_id})}\n\n"

    client = _get_client()
    system = await _build_system_prompt(mode, user_message, location)
    tools = select_tools(user_message)
    logger.debug(f"  selected {len(tools)} tools for: '{user_message[:80]}'")

    tool_call_counts: Counter = Counter()
    stuck = False

    for turn in range(settings.agent_max_turns):
        model = settings.haiku_model
        t0 = time.perf_counter()
        logger.debug(f"  stream turn {turn}: calling {model} with {len(messages)} messages")

        async with client.messages.stream(
            model=model,
            system=system,
            messages=messages,
            tools=tools,
            max_tokens=4096,
        ) as stream:
            async for text in stream.text_stream:
                yield f"event: text_delta\ndata: {json.dumps({'text': text})}\n\n"
            response = await stream.get_final_message()

        logger.debug(
            f"  stream turn {turn}: stop_reason={response.stop_reason} "
            f"in {time.perf_counter() - t0:.3f}s"
        )

        content_dicts = _content_to_dicts(response.content)
        messages.append({"role": "assistant", "content": content_dicts})
        await _save_message(pool, sid, "assistant", content_dicts)

        if response.stop_reason == "end_turn":
            break

        if response.stop_reason == "tool_use":
            tool_blocks = [b for b in response.content if b.type == "tool_use"]
            logger.debug(f"  stream turn {turn}: tool calls → {[b.name for b in tool_blocks]}")

            # Handle request_tools inline — expand tool set before signaling/executing others
            expand_results: dict[str, str] = {}
            regular_blocks = []
            for block in tool_blocks:
                if block.name == "request_tools":
                    categories = block.input.get("categories", [])
                    tools, msg = expand_tools(tools, categories)
                    expand_results[block.id] = msg
                    logger.debug(f"  stream turn {turn}: request_tools {categories} → {msg[:80]}")
                else:
                    regular_blocks.append(block)

            # Signal regular tools are starting before any execute
            for block in regular_blocks:
                yield f"event: tool_start\ndata: {json.dumps({'name': block.name})}\n\n"

            # Execute remaining tool calls concurrently
            raw_results = await asyncio.gather(*[execute_tool(b.name, b.input) for b in regular_blocks])

            tool_results = [
                {"type": "tool_result", "tool_use_id": bid, "content": msg}
                for bid, msg in expand_results.items()
            ]
            log_coros = []
            for block, tool_result in zip(regular_blocks, raw_results):
                sig = _tool_sig(block.name, block.input)
                tool_call_counts[sig] += 1
                if tool_call_counts[sig] >= 3:
                    logger.warning(f"  stream stuck loop detected: {block.name} called 3x with same args")
                    stuck_msg = (
                        f"I seem to be stuck — I've called `{block.name}` three times with the "
                        f"same arguments without making progress. Please try rephrasing your "
                        f"request or providing more specific details."
                    )
                    await _save_message(pool, sid, "assistant", [{"type": "text", "text": stuck_msg}])
                    yield f"event: text_delta\ndata: {json.dumps({'text': stuck_msg})}\n\n"
                    stuck = True
                    break

                logger.debug(
                    f"  stream turn {turn}: {block.name} {tool_result.status} in "
                    f"{tool_result.duration_ms}ms, {len(tool_result.content)} chars"
                )
                log_coros.append(pool.execute(
                    """INSERT INTO action_logs
                           (session_id, tool_name, input, output, status, error_message, duration_ms)
                       VALUES ($1, $2, $3::jsonb, $4, $5, $6, $7)""",
                    sid, block.name, json.dumps(block.input), tool_result.content,
                    tool_result.status, tool_result.error, tool_result.duration_ms,
                ))
                yield f"event: tool_done\ndata: {json.dumps({'name': block.name, 'status': tool_result.status, 'error': tool_result.error})}\n\n"
                tr: dict[str, Any] = {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": tool_result.content,
                }
                if tool_result.status == "error":
                    tr["is_error"] = True
                tool_results.append(tr)

            if stuck:
                break

            await asyncio.gather(*log_coros)
            messages.append({"role": "user", "content": tool_results})
            await _save_message(pool, sid, "user", tool_results)
        else:
            logger.debug(f"  stream turn {turn}: unexpected stop_reason, bailing")
            break

    # If the loop exhausted turns mid-tool-use, no text was streamed.
    # Do one synthesis call (no tools) and stream its output.
    if messages and messages[-1]["role"] == "user" and isinstance(messages[-1]["content"], list) and any(
        b.get("type") == "tool_result" for b in messages[-1]["content"]
    ):
        logger.debug("  stream synthesis: loop ended on tool_use, running final synthesis call")
        async with client.messages.stream(
            model=settings.haiku_model,
            system=system,
            messages=messages,
            max_tokens=1024,
        ) as stream:
            async for text in stream.text_stream:
                yield f"event: text_delta\ndata: {json.dumps({'text': text})}\n\n"
            synth = await stream.get_final_message()
        content_dicts = _content_to_dicts(synth.content)
        await _save_message(pool, sid, "assistant", content_dicts)

    await pool.execute(
        """
        UPDATE sessions
        SET last_activity = NOW(),
            message_count = (SELECT COUNT(*) FROM messages WHERE session_id = $1)
        WHERE id = $1
        """,
        sid,
    )

    logger.debug(f"stream session {session_id}: done")
    yield f"event: done\ndata: {{}}\n\n"


async def get_session(session_id: str) -> list[dict[str, Any]] | None:
    """Return all messages for a session, or None if the session doesn't exist."""
    pool = get_pool()
    sid = uuid.UUID(session_id)
    session = await pool.fetchrow("SELECT id FROM sessions WHERE id = $1", sid)
    if session is None:
        return None
    rows = await pool.fetch(
        "SELECT role, content, timestamp FROM messages WHERE session_id = $1 ORDER BY timestamp",
        sid,
    )
    return [
        {
            "role": r["role"],
            "content": json.loads(r["content"]),
            "timestamp": r["timestamp"].isoformat(),
        }
        for r in rows
    ]


async def list_sessions() -> list[dict[str, Any]]:
    """Return all sessions ordered by most recent activity."""
    pool = get_pool()
    rows = await pool.fetch(
        "SELECT id, message_count, last_activity, created_at, session_type FROM sessions ORDER BY last_activity DESC"
    )
    return [
        {
            "session_id": str(r["id"]),
            "message_count": r["message_count"],
            "last_activity": r["last_activity"].isoformat(),
            "session_type": r["session_type"] or "chat",
        }
        for r in rows
    ]
