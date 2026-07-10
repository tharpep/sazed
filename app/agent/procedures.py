"""Procedural memory — situation-to-tool-sequence recipes."""

import json
import logging
import uuid
from typing import Any

from app.agent.client import get_client, schedule_llm_log
from app.agent.json_utils import strip_json_fence
from app.agent.tools import known_tool_names
from app.config import settings
from app.db import get_pool

logger = logging.getLogger(__name__)


def _load_steps(raw_steps: Any) -> list[dict[str, Any]]:
    """asyncpg returns JSONB as a str (no codec registered) — normalize to a list."""
    if isinstance(raw_steps, str):
        return json.loads(raw_steps)
    return raw_steps or []


async def load_relevant_procedures(
    user_message: str, limit: int | None = None
) -> list[dict[str, Any]]:
    """Return active procedures whose trigger_keywords match the user message.

    Keyword prefilter only — no vector search available in sazed. Skips procedures
    whose steps reference a tool that no longer exists (stale-procedure guard).
    Bumps use_count/last_used_at for anything returned.
    """
    pool = get_pool()
    rows = await pool.fetch(
        "SELECT id, name, trigger_desc, trigger_keywords, steps, use_count "
        "FROM procedures WHERE status = 'active' ORDER BY use_count DESC, created_at DESC"
    )

    msg_lower = user_message.lower()
    known = known_tool_names()
    matched: list[dict[str, Any]] = []
    for row in rows:
        keywords = row["trigger_keywords"] or []
        if not any(kw.lower() in msg_lower for kw in keywords):
            continue
        steps = [s for s in _load_steps(row["steps"]) if s.get("tool") in known]
        if not steps:
            logger.debug(
                f"load_relevant_procedures: skipping '{row['name']}' — no valid steps left"
            )
            continue
        matched.append({**dict(row), "steps": steps})

    matched = matched[: limit or settings.max_procedures_in_prompt]

    if matched:
        await pool.execute(
            "UPDATE procedures SET use_count = use_count + 1, last_used_at = NOW() "
            "WHERE id = ANY($1::uuid[])",
            [p["id"] for p in matched],
        )
        logger.debug(f"load_relevant_procedures: matched {[p['name'] for p in matched]}")

    return matched


def format_procedures_for_prompt(procedures: list[dict[str, Any]]) -> str:
    """Render matched procedures as a compact system-prompt block."""
    lines = [
        "## Known procedures",
        "Suggested step sequences for situations Sazed has handled successfully before. "
        "Treat as guidance, not a script — verify each step still applies before following it.",
    ]
    for proc in procedures:
        lines.append(f"\n**{proc['name']}** — {proc['trigger_desc']}")
        for i, step in enumerate(proc["steps"], 1):
            hint = f" ({step['param_hints']})" if step.get("param_hints") else ""
            lines.append(f"{i}. {step['tool']} — {step.get('purpose', '')}{hint}")
    return "\n".join(lines)


def _parse_json_object(text: str) -> dict[str, Any] | None:
    """Parse a single JSON object from LLM output, handling markdown fences and `null`."""
    try:
        result = json.loads(strip_json_fence(text))
        return result if isinstance(result, dict) else None
    except json.JSONDecodeError:
        return None


def _format_action_log_steps(action_logs: list[dict[str, Any]]) -> str:
    """Render the session's ordered tool calls for the proposal prompt."""
    if not action_logs:
        return "(no tool calls)"
    lines = []
    for row in action_logs:
        raw_input = row["input"]
        try:
            args = json.loads(raw_input) if isinstance(raw_input, str) else raw_input
        except (json.JSONDecodeError, TypeError):
            args = raw_input
        lines.append(f"- {row['tool_name']}({args}) → {row['status']}")
    return "\n".join(lines)


async def propose_procedure_from_session(
    session_id: str,
    conversation_text: str,
    action_logs: list[dict[str, Any]],
    existing_names: list[str],
) -> dict[str, Any] | None:
    """Ask Haiku whether a session's tool sequence is a reusable recipe.

    Inserts a status='proposed' row and returns it, or returns None if Haiku
    declines (outputs null) or the response has no valid, known-tool steps.
    """
    steps_preview = _format_action_log_steps(action_logs)
    existing = ", ".join(existing_names) or "(none)"

    prompt = f"""You are identifying reusable task recipes for a personal AI assistant.

Given the tool calls made during a session and the surrounding conversation, decide whether
this represents a recurring, reusable workflow (e.g. "summarize my week", "prep my Monday",
"file a receipt") worth remembering as a named procedure.

Do NOT propose a procedure for a one-off or highly conversation-specific task.
Do NOT duplicate an existing procedure. Existing procedure names: {existing}

If this session IS a good candidate, return a JSON object exactly in this shape:
{{"name": "short_snake_case_name", "trigger_desc": "one sentence: when to use this",
  "trigger_keywords": ["keyword1", "keyword2"],
  "steps": [{{"tool": "exact_tool_name", "purpose": "why this step",
              "param_hints": "optional notes"}}]}}

If this session is NOT a good candidate, return exactly: null

Return only the JSON value, no other text.

Tool calls made this session, in order:
{steps_preview}

Conversation:
{conversation_text}"""

    response = await get_client().messages.create(
        model=settings.haiku_model,
        max_tokens=768,
        messages=[{"role": "user", "content": prompt}],
    )
    if settings.llm_cost_tracking:
        schedule_llm_log(session_id, None, settings.haiku_model, "procedure", response)
    proposal = _parse_json_object(response.content[0].text)
    if not proposal:
        return None

    known = known_tool_names()
    steps = [s for s in proposal.get("steps", []) if isinstance(s, dict) and s.get("tool") in known]
    if not steps or not proposal.get("name") or not proposal.get("trigger_desc"):
        logger.debug(
            f"propose_procedure_from_session: rejected malformed/empty proposal for {session_id}"
        )
        return None

    pool = get_pool()
    row = await pool.fetchrow(
        "INSERT INTO procedures (name, trigger_desc, trigger_keywords, steps, source_session) "
        "VALUES ($1, $2, $3, $4::jsonb, $5) "
        "RETURNING id, name, trigger_desc, trigger_keywords, steps, status, "
        "source_session, use_count, created_at, last_used_at",
        proposal["name"],
        proposal["trigger_desc"],
        proposal.get("trigger_keywords", []),
        json.dumps(steps),
        uuid.UUID(session_id),
    )
    logger.info(f"procedures: proposed '{proposal['name']}' from session {session_id}")
    return dict(row)


async def list_procedures(status: str | None = None) -> list[dict[str, Any]]:
    """Return procedures, optionally filtered by status ('proposed' | 'active' | 'archived')."""
    pool = get_pool()
    if status:
        rows = await pool.fetch(
            "SELECT id, name, trigger_desc, trigger_keywords, steps, status, "
            "source_session, use_count, created_at, last_used_at "
            "FROM procedures WHERE status = $1 ORDER BY created_at DESC",
            status,
        )
    else:
        rows = await pool.fetch(
            "SELECT id, name, trigger_desc, trigger_keywords, steps, status, "
            "source_session, use_count, created_at, last_used_at "
            "FROM procedures ORDER BY created_at DESC"
        )
    return [dict(row) for row in rows]


async def activate_procedure(procedure_id: str) -> bool:
    pool = get_pool()
    result = await pool.execute(
        "UPDATE procedures SET status = 'active' WHERE id = $1", uuid.UUID(procedure_id)
    )
    return result == "UPDATE 1"


async def archive_procedure(procedure_id: str) -> bool:
    pool = get_pool()
    result = await pool.execute(
        "UPDATE procedures SET status = 'archived' WHERE id = $1", uuid.UUID(procedure_id)
    )
    return result == "UPDATE 1"


async def delete_procedure(procedure_id: str) -> bool:
    pool = get_pool()
    result = await pool.execute("DELETE FROM procedures WHERE id = $1", uuid.UUID(procedure_id))
    return result == "DELETE 1"
