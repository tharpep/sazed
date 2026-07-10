"""Procedural memory — situation-to-tool-sequence recipes."""

import json
import logging
import uuid
from typing import Any

from app.agent.client import get_client
from app.agent.tools import known_tool_names
from app.config import settings
from app.db import get_pool

logger = logging.getLogger(__name__)


def _load_steps(raw_steps: Any) -> list[dict[str, Any]]:
    """asyncpg returns JSONB as a str (no codec registered) — normalize to a list."""
    if isinstance(raw_steps, str):
        return json.loads(raw_steps)
    return raw_steps or []


async def load_relevant_procedures(user_message: str, limit: int | None = None) -> list[dict[str, Any]]:
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
            logger.debug(f"load_relevant_procedures: skipping '{row['name']}' — no valid steps left")
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
