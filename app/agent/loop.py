"""Agent loop — core reasoning cycle."""

import uuid
from datetime import date
from typing import Any

import anthropic

from app.agent.memory import format_for_prompt, load_memory
from app.agent.tools import execute_tool, get_tool_schemas
from app.config import settings

MAX_TURNS = 5

# In-memory session storage. Replaced with asyncpg reads/writes in Phase 3.3.
_sessions: dict[str, list[dict]] = {}

_client: anthropic.AsyncAnthropic | None = None


def _get_client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    return _client


def _build_system_prompt() -> str:
    today = date.today().strftime("%A, %B %d, %Y")
    memory_section = format_for_prompt(load_memory())
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


async def run_turn(session_id: str | None, user_message: str) -> tuple[str, str]:
    """
    Run one user turn through the agent loop.
    Returns (session_id, response_text).
    """
    if not session_id:
        session_id = str(uuid.uuid4())

    messages = list(_sessions.get(session_id, []))
    messages.append({"role": "user", "content": user_message})

    client = _get_client()
    system = _build_system_prompt()
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
        else:
            # Unexpected stop reason — bail out
            break

    _sessions[session_id] = messages
    return session_id, _extract_text(final_content)


# ---------------------------------------------------------------------------
# Session accessors — replaced with asyncpg queries in Phase 3.3
# ---------------------------------------------------------------------------

def get_session(session_id: str) -> list[dict[str, Any]] | None:
    return _sessions.get(session_id)


def list_sessions() -> list[dict[str, Any]]:
    return [
        {"session_id": sid, "message_count": len(msgs)}
        for sid, msgs in _sessions.items()
    ]
