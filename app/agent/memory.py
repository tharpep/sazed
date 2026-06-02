"""Structured memory — agent_memory store and helpers."""

import logging
import re
import uuid
from collections import defaultdict
from typing import Any

from app.db import get_pool

logger = logging.getLogger(__name__)

_MEMORY_PATTERNS: dict[str, re.Pattern] = {
    "preference": re.compile(
        r'\b(prefer|like|want|style|format|how i|my (preferred|favorite|usual|default)'
        r'|always (do|use|write|format)|never (do|use)|instead of|rather than)\b', re.I),
    "project": re.compile(
        r'\b(project|work|build|coding|develop|startup|intern|job|goal|objective'
        r'|current(ly)?|working on|building|making|side.?project)\b', re.I),
    "relationship": re.compile(
        r'\b(person|people|friend|colleague|team|partner|contact|collaborat'
        r'|who is|who are|my (boss|manager|coworker|advisor|professor|mentor))\b', re.I),
}

_ALWAYS_MEMORY_CATEGORIES: frozenset[str] = frozenset({"personal", "instruction"})


async def load_memory() -> list[dict[str, Any]]:
    """Return all facts sorted by most recently updated."""
    pool = get_pool()
    rows = await pool.fetch(
        "SELECT id, fact_type, key, value, confidence, source, created_at, updated_at "
        "FROM agent_memory ORDER BY updated_at DESC"
    )
    logger.debug(f"load_memory: {len(rows)} fact(s)")
    return [dict(row) for row in rows]


async def load_relevant_memory(user_message: str) -> list[dict[str, Any]]:
    """Load only the fact categories relevant to the current user message.

    personal and instruction are always included.
    preference, project, relationship are pattern-matched from the message.
    """
    categories = set(_ALWAYS_MEMORY_CATEGORIES)
    for cat, pat in _MEMORY_PATTERNS.items():
        if pat.search(user_message):
            categories.add(cat)

    pool = get_pool()
    rows = await pool.fetch(
        "SELECT id, fact_type, key, value, confidence, source, created_at, updated_at "
        "FROM agent_memory WHERE fact_type = ANY($1::text[]) ORDER BY updated_at DESC",
        list(categories),
    )
    logger.debug(f"load_relevant_memory: {len(rows)} fact(s) from categories {categories}")
    return [dict(row) for row in rows]


async def upsert_fact(
    fact_type: str,
    key: str,
    value: str,
    confidence: float = 1.0,
    source: str = "user_explicit",
) -> dict[str, Any]:
    """
    Insert or update a fact by (fact_type, key).
    Only overwrites an existing fact's value if new confidence >= existing confidence.
    """
    pool = get_pool()
    logger.debug(f"upsert_fact: [{fact_type}] {key}={value!r} confidence={confidence} source={source}")
    row = await pool.fetchrow(
        """
        INSERT INTO agent_memory (fact_type, key, value, confidence, source)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (fact_type, key) DO UPDATE
            SET value      = CASE WHEN EXCLUDED.confidence >= agent_memory.confidence
                                  THEN EXCLUDED.value      ELSE agent_memory.value      END,
                confidence = CASE WHEN EXCLUDED.confidence >= agent_memory.confidence
                                  THEN EXCLUDED.confidence ELSE agent_memory.confidence END,
                source     = CASE WHEN EXCLUDED.confidence >= agent_memory.confidence
                                  THEN EXCLUDED.source     ELSE agent_memory.source     END,
                updated_at = CASE WHEN EXCLUDED.confidence >= agent_memory.confidence
                                  THEN NOW()               ELSE agent_memory.updated_at END
        RETURNING id, fact_type, key, value, confidence, source, created_at, updated_at
        """,
        fact_type, key, value, confidence, source,
    )
    return dict(row)


async def delete_fact(memory_id: str) -> bool:
    """Delete a fact by UUID. Returns False if not found."""
    pool = get_pool()
    result = await pool.execute(
        "DELETE FROM agent_memory WHERE id = $1", uuid.UUID(memory_id)
    )
    return result == "DELETE 1"


def format_for_prompt(facts: list[dict[str, Any]]) -> str:
    """Format facts into a system prompt section, grouped by fact_type."""
    if not facts:
        return "(None yet)"

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for fact in facts:
        grouped[fact["fact_type"]].append(fact)

    lines = []
    for fact_type, items in grouped.items():
        lines.append(f"**{fact_type.capitalize()}**")
        for item in items:
            lines.append(f"- {item['key']}: {item['value']}")
        lines.append("")

    return "\n".join(lines).strip()
