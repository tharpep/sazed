"""Think loop — autonomous proactive reasoning cycle."""

import asyncio
import json
import logging
import time
import uuid
from collections import Counter
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import anthropic

from app.agent.memory import format_for_prompt, load_memory
from app.agent.session import compress_context
from app.agent.tools import execute_tool, get_think_tool_schemas
from app.config import settings
from app.db import get_pool

logger = logging.getLogger(__name__)

# Stable session IDs per context bucket — persist across days so Sazed
# remembers what it already surfaced and avoids repeating itself.
_THINK_SESSIONS: dict[str, str] = {
    "morning": str(uuid.uuid5(uuid.NAMESPACE_DNS, "think-morning")),
    "midday":  str(uuid.uuid5(uuid.NAMESPACE_DNS, "think-midday")),
    "evening": str(uuid.uuid5(uuid.NAMESPACE_DNS, "think-evening")),
}
_THINK_SESSION_DEFAULT = str(uuid.uuid5(uuid.NAMESPACE_DNS, "think-default"))

_WRITE_TOOLS = frozenset({
    "send_notification", "create_task", "memory_update",
    "append_to_file", "create_file", "sync_kb",
})

_CONTEXT_DESCRIPTIONS = {
    "morning": (
        "It is morning. Focus on: today's calendar events and their importance, tasks due today, "
        "any overnight emails worth flagging. Surface the single most useful thing for the day ahead."
    ),
    "midday": (
        "It is midday. Focus on: whether tasks due today are on track, any new emails since morning "
        "that need attention, anything time-sensitive this afternoon."
    ),
    "evening": (
        "It is evening. Focus on: reflecting on what was on today's agenda, looking ahead to tomorrow, "
        "anything worth writing to Drive to preserve context from today."
    ),
}


_client: anthropic.AsyncAnthropic | None = None


def _get_client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    return _client


def _tool_sig(name: str, args: dict) -> tuple:
    return (name, tuple(sorted((k, str(v)) for k, v in args.items())))


def _build_think_system_prompt(
    context: str | None,
    trigger: str | None,
    memory_section: str,
) -> list[dict[str, Any]]:
    context_desc = _CONTEXT_DESCRIPTIONS.get(context or "", (
        "Run a general proactive check — calendar, tasks, emails, and any other context "
        "relevant to my current goals and projects."
    ))
    if trigger:
        context_desc += f"\n\nThis think was triggered by: {trigger}"

    blocks: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": (
                "You are Sazed, running in autonomous think mode. No user is present.\n\n"
                "## Your job\n"
                "Check context, notice anything worth acting on, then act — or stay silent.\n\n"
                "## What to do\n"
                "1. Pull relevant context (calendar, tasks, emails, KB, GitHub, finance as needed).\n"
                "2. Cross-reference with what you know about the user's goals, projects, and preferences.\n"
                "3. Decide what (if anything) to do:\n"
                "   - Something time-sensitive or important to surface → send_notification "
                "(max 1 per session, plain text, ≤2 sentences, no greeting or sign-off)\n"
                "   - A specific upcoming thing to remind the user of → create_task with a due datetime "
                "in an appropriate list (a 'Reminders' list if one exists, otherwise create it)\n"
                "   - A durable fact worth preserving → memory_update\n"
                "   - A longer observation worth keeping → append_to_file on "
                "'Sazed Think Log.md' in Drive (find it with list_files, or create it), then sync_kb\n"
                "4. If nothing is genuinely worth doing, do nothing. "
                "Silence is the correct answer most of the time.\n\n"
                "## Rules\n"
                "- Max 1 notification per think session. Make it count.\n"
                "- Check this session's message history — do not repeat what you already surfaced.\n"
                "- Notifications: plain text only, no markdown, no greetings, no filler.\n"
                "- Be selective. A think session that does nothing is better than one that spams."
            ),
            "cache_control": {"type": "ephemeral"},
        },
        {
            "type": "text",
            "text": f"## Known facts about the user\n{memory_section}",
            "cache_control": {"type": "ephemeral"},
        },
        {
            "type": "text",
            "text": f"## Current context\n{context_desc}",
        },
    ]
    return blocks


async def _apply_context_window(
    pool, session_id: uuid.UUID, messages: list[dict[str, Any]]
) -> list[dict[str, Any]]:
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
        {"role": "user", "content": f"[Context from earlier think sessions]\n{summary}"},
        {"role": "assistant", "content": "Understood."},
    ]
    return summary_pair + recent


async def _save_message(pool, session_id: uuid.UUID, role: str, content: Any) -> None:
    await pool.execute(
        "INSERT INTO messages (session_id, role, content) VALUES ($1, $2, $3)",
        session_id, role, json.dumps(content),
    )


def _content_to_dicts(content: list) -> list[dict[str, Any]]:
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
    return "No action taken."


async def run_think(
    session_id: str | None,
    context: str | None,
    trigger: str | None,
    timezone: str | None,
) -> tuple[str, bool, str]:
    """
    Run one autonomous think turn.
    Returns (session_id, acted, summary).
    acted=True if any write tool was called.
    """
    session_id = session_id or _THINK_SESSIONS.get(context or "", _THINK_SESSION_DEFAULT)

    pool = get_pool()
    sid = uuid.UUID(session_id)

    await pool.execute(
        "INSERT INTO sessions (id, session_type) VALUES ($1, 'think') ON CONFLICT DO NOTHING",
        sid,
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
        tz = ZoneInfo(timezone) if timezone else ZoneInfo("America/New_York")
    except ZoneInfoNotFoundError:
        tz = ZoneInfo("America/New_York")
    now = datetime.now(tz)
    tz_prefix = f"[{now.strftime('%A, %B %d, %Y')} {now.strftime('%I:%M %p')} {tz.key}]\n"

    # The "user turn" that kicks off think is an internal prompt, not a real user message.
    think_prompt = f"{tz_prefix}[Autonomous think triggered — context: {context or 'general'}]"
    messages.append({"role": "user", "content": think_prompt})
    await _save_message(pool, sid, "user", think_prompt)

    logger.debug(f"think session {session_id}: starting, context={context}")

    memory_section = format_for_prompt(await load_memory())
    system = _build_think_system_prompt(context, trigger, memory_section)
    tools = get_think_tool_schemas()
    client = _get_client()

    tool_call_counts: Counter = Counter()
    final_content: list[dict[str, Any]] = []
    write_tools_called: set[str] = set()
    stuck = False

    for turn in range(4):
        t0 = time.perf_counter()
        logger.debug(f"  think turn {turn}: calling {settings.haiku_model}")
        response = await client.messages.create(
            model=settings.haiku_model,
            system=system,
            messages=messages,
            tools=tools,
            max_tokens=2048,
        )
        logger.debug(
            f"  think turn {turn}: stop_reason={response.stop_reason} "
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
            logger.debug(f"  think turn {turn}: tool calls → {[b.name for b in tool_blocks]}")

            raw_results = await asyncio.gather(*[execute_tool(b.name, b.input) for b in tool_blocks])

            tool_results = []
            log_coros = []
            for block, tool_result in zip(tool_blocks, raw_results):
                sig = _tool_sig(block.name, block.input)
                tool_call_counts[sig] += 1
                if tool_call_counts[sig] >= 3:
                    logger.warning(f"  think stuck loop: {block.name} called 3x with same args")
                    stuck = True
                    break

                if block.name in _WRITE_TOOLS:
                    write_tools_called.add(block.name)

                logger.debug(
                    f"  think turn {turn}: {block.name} {tool_result.status} in "
                    f"{tool_result.duration_ms}ms"
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
            break

    await pool.execute(
        """
        UPDATE sessions
        SET last_activity = NOW(),
            message_count = (SELECT COUNT(*) FROM messages WHERE session_id = $1)
        WHERE id = $1
        """,
        sid,
    )

    acted = bool(write_tools_called)
    summary = _extract_text(final_content)
    logger.debug(
        f"think session {session_id}: done, acted={acted}, "
        f"write_tools={write_tools_called}, summary='{summary[:120]}'"
    )
    return session_id, acted, summary
