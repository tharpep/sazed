"""Agent loop — core reasoning cycle."""

import json
import uuid
from datetime import date
from typing import Any

import anthropic

from app.agent.memory import format_for_prompt, load_memory
from app.agent.tools import execute_tool, get_tool_schemas
from app.config import settings
from app.db import get_pool

MAX_TURNS = 5

_client: anthropic.AsyncAnthropic | None = None


def _get_client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    return _client


async def _build_system_prompt() -> str:
    today = date.today().strftime("%A, %B %d, %Y")
    memory_section = format_for_prompt(await load_memory())
    return f"""You are Sazed, a personal AI assistant.
Today is {today}.

## Behavior
- Be direct and concise. No preamble.
- Use tools to get real data before answering questions about calendar, tasks, or email.
- When the user explicitly asks you to remember something, call memory_update immediately.
- If a tool call fails, say so clearly and suggest what to try instead.

## Known facts about the user
{memory_section}
"""


def _select_model(message: str, turn: int) -> str:
    if turn > 2 or len(message) > 500:
        return settings.sonnet_model
    return settings.haiku_model


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


async def _save_message(pool, session_id: uuid.UUID, role: str, content: Any) -> None:
    """Persist one message row. Content is JSON-encoded to handle both strings and block lists."""
    await pool.execute(
        "INSERT INTO messages (session_id, role, content) VALUES ($1, $2, $3)",
        session_id, role, json.dumps(content),
    )


async def run_turn(session_id: str | None, user_message: str) -> tuple[str, str]:
    """
    Run one user turn through the agent loop.
    Returns (session_id, response_text).
    """
    if not session_id:
        session_id = str(uuid.uuid4())

    pool = get_pool()
    sid = uuid.UUID(session_id)

    # Ensure the session row exists
    await pool.execute(
        "INSERT INTO sessions (id) VALUES ($1) ON CONFLICT DO NOTHING", sid
    )

    # Load existing history
    rows = await pool.fetch(
        "SELECT role, content FROM messages WHERE session_id = $1 ORDER BY timestamp",
        sid,
    )
    messages: list[dict[str, Any]] = [
        {"role": r["role"], "content": json.loads(r["content"])} for r in rows
    ]

    # Append and persist the new user message
    messages.append({"role": "user", "content": user_message})
    await _save_message(pool, sid, "user", user_message)

    client = _get_client()
    system = await _build_system_prompt()
    model = _select_model(user_message, turn=0)
    final_content: list[dict[str, Any]] = []

    for turn in range(MAX_TURNS):
        response = await client.messages.create(
            model=model,
            system=system,
            messages=messages,
            tools=get_tool_schemas(),
            max_tokens=4096,
        )

        content_dicts = _content_to_dicts(response.content)
        messages.append({"role": "assistant", "content": content_dicts})
        await _save_message(pool, sid, "assistant", content_dicts)
        final_content = content_dicts

        if response.stop_reason == "end_turn":
            break

        if response.stop_reason == "tool_use":
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    result = await execute_tool(block.name, block.input)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })
            messages.append({"role": "user", "content": tool_results})
            await _save_message(pool, sid, "user", tool_results)
        else:
            # Unexpected stop reason — bail out
            break

    # Update session stats
    await pool.execute(
        """
        UPDATE sessions
        SET last_activity = NOW(),
            message_count = (SELECT COUNT(*) FROM messages WHERE session_id = $1)
        WHERE id = $1
        """,
        sid,
    )

    return session_id, _extract_text(final_content)


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
        "SELECT id, message_count, last_activity, created_at FROM sessions ORDER BY last_activity DESC"
    )
    return [
        {
            "session_id": str(r["id"]),
            "message_count": r["message_count"],
            "last_activity": r["last_activity"].isoformat(),
        }
        for r in rows
    ]
