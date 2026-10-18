"""Shared Anthropic client singleton and common agent utilities."""

import asyncio
import logging
import uuid
from typing import Any

import anthropic

from app.config import settings
from app.db import get_pool

logger = logging.getLogger(__name__)

_client: anthropic.AsyncAnthropic | None = None

# Recognized `purpose` values for log_llm_call / schedule_llm_log — free-form strings,
# not an enum, but every call site should use one of these so `GET /audit/metrics`'s
# by_purpose rollup doesn't fragment on a typo. Update this list when adding a call site.
# chat | synthesis | confirm | title | context_compress | facts | summary
# | kb_summary | procedure | think


def get_client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    return _client


# Sonnet 5.5 only: thinking blocks dropped (not 400'd) when earlier history or the tool list
# changed since they were produced (the loop expands tools mid-request), plus server-side
# refusal fallback. Sent as raw headers/body because the pinned SDK (0.49) predates them.
_SONNET_BETAS = "thinking-binding-controls-2026-08-01,server-side-fallback-2026-07-01"


def model_params(model: str) -> dict[str, Any]:
    """Per-model kwargs for messages.create / messages.stream.

    Both 5.5 models think by default. Haiku runs with thinking off, matching Haiku 4.5's
    behaviour, so short utility calls (max_tokens=5 etc.) and `content[0].text` reads keep
    working. Sonnet is the escalation path, so it keeps adaptive thinking at medium effort;
    callers must size max_tokens for thinking and pass thinking blocks back within a request.
    """
    if model == settings.haiku_model:
        return {"thinking": {"type": "disabled"}}
    if model == settings.sonnet_model:
        return {
            "extra_headers": {"anthropic-beta": _SONNET_BETAS},
            "extra_body": {
                "thinking": {
                    "type": "adaptive",
                    "block_binding": {"prefix_mismatch_behavior": "drop_block"},
                },
                "output_config": {"effort": "medium"},
                "fallbacks": "default",
            },
        }
    return {}


def with_message_cache(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return a copy of `messages` with a cache breakpoint on the last block of the last message.

    Lets the tool loop cache the growing history. Only the copy is annotated — `messages`
    itself (and what gets persisted) is left untouched. Callers must keep the request at
    4 or fewer breakpoints in total (system blocks + last tool schema + this one).
    """
    if not messages:
        return messages
    last = messages[-1]
    content = last["content"]
    blocks = [{"type": "text", "text": content}] if isinstance(content, str) else list(content)
    # Thinking blocks can't carry cache_control; skip rather than risk a 400.
    if not blocks or blocks[-1].get("type") in ("thinking", "redacted_thinking"):
        return messages
    blocks[-1] = {**blocks[-1], "cache_control": {"type": "ephemeral"}}
    return messages[:-1] + [{"role": last["role"], "content": blocks}]


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

    Call via `schedule_llm_log(...)` below, not a bare `asyncio.create_task(...)` —
    an unreferenced task can be garbage-collected mid-write.
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
    except Exception:
        logger.warning(f"log_llm_call failed (purpose={purpose}, model={model})", exc_info=True)


# asyncio only holds a weak reference to a task — with nothing else referencing it,
# a fire-and-forget task can be garbage-collected before it finishes. Keep a strong
# reference here until each task completes, then let the done-callback drop it.
_pending_log_tasks: set[asyncio.Task] = set()


def schedule_llm_log(
    session_id: uuid.UUID | str | None,
    turn: int | None,
    model: str,
    purpose: str,
    response: Any,
    duration_ms: int | None = None,
) -> None:
    """Fire-and-forget wrapper around log_llm_call — never blocks the caller."""
    task = asyncio.create_task(
        log_llm_call(session_id, turn, model, purpose, response, duration_ms)
    )
    _pending_log_tasks.add(task)
    task.add_done_callback(_pending_log_tasks.discard)
