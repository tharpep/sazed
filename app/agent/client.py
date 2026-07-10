"""Shared Anthropic client singleton and common agent utilities."""

import logging
import uuid
from typing import Any

import anthropic

from app.config import settings
from app.db import get_pool

logger = logging.getLogger(__name__)

_client: anthropic.AsyncAnthropic | None = None


def get_client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    return _client


def tool_sig(name: str, args: dict) -> tuple:
    """Stable hashable signature for a tool call — used for stuck loop detection."""
    return (name, tuple(sorted((k, str(v)) for k, v in args.items())))


async def log_llm_call(
    session_id: uuid.UUID | str | None,
    turn: int | None,
    model: str,
    purpose: str,
    response: Any,
    duration_ms: int | None = None,
) -> None:
    """Persist token usage for one LLM call. Never raises — logs and swallows on failure.

    Callers dispatch this via `asyncio.create_task(...)` so it never blocks the
    request path (per the plan's fire-and-forget requirement) — this function
    itself is a plain coroutine, not a task-spawning wrapper.
    """
    try:
        usage = response.usage
        sid = uuid.UUID(session_id) if isinstance(session_id, str) else session_id
        pool = get_pool()
        await pool.execute(
            "INSERT INTO llm_calls "
            "(session_id, turn, model, purpose, input_tokens, output_tokens, "
            " cache_read_tokens, cache_write_tokens, duration_ms) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)",
            sid,
            turn,
            model,
            purpose,
            usage.input_tokens,
            usage.output_tokens,
            getattr(usage, "cache_read_input_tokens", None),
            getattr(usage, "cache_creation_input_tokens", None),
            duration_ms,
        )
    except Exception as e:
        logger.warning(f"log_llm_call failed (purpose={purpose}, model={model}): {e}")
