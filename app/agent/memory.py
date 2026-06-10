"""Structured memory — agent_memory store and helpers."""

import logging
import re
import uuid
from collections import defaultdict
from datetime import UTC, datetime, timedelta
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

    - New fact: plain INSERT with valid_from = now.
    - Same value: bump last_confirmed; raise confidence if new value is higher.
    - Different value: new value wins when confidence >= existing OR source is
      user_explicit/api (direct user statement always overrides older inferences).
      On a win the old row is archived to agent_memory_history before updating the live row.
    """
    pool = get_pool()
    logger.debug(f"upsert_fact: [{fact_type}] {key}={value!r} conf={confidence} src={source}")

    async with pool.acquire() as conn:
        async with conn.transaction():
            existing = await conn.fetchrow(
                "SELECT id, fact_type, key, value, confidence, source, "
                "created_at, updated_at, valid_from, last_confirmed "
                "FROM agent_memory WHERE fact_type = $1 AND key = $2",
                fact_type, key,
            )

            if existing is None:
                row = await conn.fetchrow(
                    "INSERT INTO agent_memory "
                    "(fact_type, key, value, confidence, source, valid_from, last_confirmed) "
                    "VALUES ($1, $2, $3, $4, $5, NOW(), NOW()) "
                    "RETURNING id, fact_type, key, value, confidence, "
                    "source, created_at, updated_at",
                    fact_type, key, value, confidence, source,
                )

            elif existing["value"] == value:
                new_conf = max(confidence, float(existing["confidence"]))
                row = await conn.fetchrow(
                    "UPDATE agent_memory SET last_confirmed = NOW(), confidence = $1 "
                    "WHERE fact_type = $2 AND key = $3 "
                    "RETURNING id, fact_type, key, value, confidence, "
                    "source, created_at, updated_at",
                    new_conf, fact_type, key,
                )

            else:
                # user_explicit/api sources always win — a direct user correction must
                # override even a high-confidence inferred fact.
                new_wins = (
                    confidence >= float(existing["confidence"])
                    or source in ("user_explicit", "api")
                )
                if new_wins:
                    valid_from = existing["valid_from"] or existing["created_at"]
                    await conn.execute(
                        "INSERT INTO agent_memory_history "
                        "(fact_type, key, value, confidence, source, "
                        "valid_from, valid_to, superseded_by) "
                        "VALUES ($1, $2, $3, $4, $5, $6, NOW(), $7)",
                        existing["fact_type"], existing["key"], existing["value"],
                        float(existing["confidence"]), existing["source"],
                        valid_from, value,
                    )
                    row = await conn.fetchrow(
                        "UPDATE agent_memory "
                        "SET value = $1, confidence = $2, source = $3, "
                        "updated_at = NOW(), valid_from = NOW(), last_confirmed = NOW() "
                        "WHERE fact_type = $4 AND key = $5 "
                        "RETURNING id, fact_type, key, value, confidence, "
                        "source, created_at, updated_at",
                        value, confidence, source, fact_type, key,
                    )
                else:
                    # New value loses — return existing fact unchanged (same shape as RETURNING).
                    keys = (
                        "id", "fact_type", "key", "value",
                        "confidence", "source", "created_at", "updated_at",
                    )
                    return {k: existing[k] for k in keys}

    return dict(row)


async def memory_history(fact_type: str, key: str) -> list[dict[str, Any]]:
    """Return superseded values for a fact, newest-superseded-first."""
    pool = get_pool()
    rows = await pool.fetch(
        "SELECT id, fact_type, key, value, confidence, source, valid_from, valid_to, superseded_by "
        "FROM agent_memory_history WHERE fact_type = $1 AND key = $2 ORDER BY valid_to DESC",
        fact_type, key,
    )
    return [dict(row) for row in rows]


async def load_stale_memory(days: int = 30) -> list[dict[str, Any]]:
    """Return live facts whose last_confirmed is older than `days` days."""
    pool = get_pool()
    cutoff = datetime.now(UTC) - timedelta(days=days)
    rows = await pool.fetch(
        "SELECT id, fact_type, key, value, confidence, source, "
        "created_at, updated_at, valid_from, last_confirmed "
        "FROM agent_memory WHERE last_confirmed < $1 ORDER BY last_confirmed ASC",
        cutoff,
    )
    logger.debug(f"load_stale_memory: {len(rows)} fact(s) older than {days} days")
    return [dict(row) for row in rows]


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
