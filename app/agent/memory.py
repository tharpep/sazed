"""Structured memory — agent_memory store and helpers."""

import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

# In-memory store keyed by (fact_type, key) — mirrors the UNIQUE(fact_type, key)
# constraint from the DB schema. Replaced with asyncpg reads/writes in Phase 3.3.
_store: dict[tuple[str, str], dict[str, Any]] = {}


def load_memory() -> list[dict[str, Any]]:
    """Return all facts sorted by most recently updated."""
    return sorted(_store.values(), key=lambda f: f["updated_at"], reverse=True)


def upsert_fact(
    fact_type: str,
    key: str,
    value: str,
    confidence: float = 1.0,
    source: str = "user_explicit",
) -> dict[str, Any]:
    """
    Insert or update a fact by (fact_type, key).
    Only overwrites an existing fact if new confidence >= existing confidence.
    """
    existing = _store.get((fact_type, key))
    now = datetime.now(timezone.utc).isoformat()

    if existing is None:
        fact: dict[str, Any] = {
            "id": str(uuid.uuid4()),
            "fact_type": fact_type,
            "key": key,
            "value": value,
            "confidence": confidence,
            "source": source,
            "created_at": now,
            "updated_at": now,
        }
        _store[(fact_type, key)] = fact
        return fact

    if confidence >= existing["confidence"]:
        existing["value"] = value
        existing["confidence"] = confidence
        existing["source"] = source
        existing["updated_at"] = now

    return existing


def delete_fact(memory_id: str) -> bool:
    """Delete a fact by UUID. Returns False if not found."""
    key = next((k for k, v in _store.items() if v["id"] == memory_id), None)
    if key is None:
        return False
    del _store[key]
    return True


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
