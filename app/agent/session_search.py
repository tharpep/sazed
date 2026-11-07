"""search_past_sessions: full-text search over stored conversation messages (INTERNAL tool;
definition lives in agent/tools.py). Backed by the GIN index from migration 013."""

import json
import re
from typing import Any

from app.db import get_pool

SESSION_SEARCH_TOOL_NAMES = frozenset({"search_past_sessions"})

_DEFAULT_LIMIT = 8
_MAX_LIMIT = 20
_SNIPPET_CHARS = 400


def _plain_text(content: str) -> str:
    """Messages are stored as JSON content blocks; keep only the text a person would read."""
    try:
        data = json.loads(content)
    except ValueError:
        return content
    if isinstance(data, str):
        return data
    if isinstance(data, list):
        parts = [b.get("text", "") for b in data if isinstance(b, dict) and b.get("type") == "text"]
        return "\n".join(p for p in parts if p)
    return content


def _snippet(text: str, query: str) -> str:
    """A window of the text around the first query word that appears in it."""
    text = re.sub(r"\s+", " ", text).strip()
    start = 0
    for word in re.findall(r"\w+", query.lower()):
        idx = text.lower().find(word)
        if idx >= 0:
            start = max(0, idx - _SNIPPET_CHARS // 4)
            break
    chunk = text[start : start + _SNIPPET_CHARS]
    return ("…" if start else "") + chunk + ("…" if start + _SNIPPET_CHARS < len(text) else "")


async def handle(name: str, args: dict[str, Any]) -> str:
    try:
        query = (args.get("query") or "").strip()
        if not query:
            return "Error: query is required"
        limit = min(max(int(args.get("limit") or _DEFAULT_LIMIT), 1), _MAX_LIMIT)
        rows = await get_pool().fetch(
            """
            SELECT m.session_id, m.role, m.content, m.timestamp,
                   ts_rank(m.content_tsv, q.query) AS rank
            FROM messages m, websearch_to_tsquery('english', $1) AS q(query)
            WHERE m.content_tsv @@ q.query AND m.role IN ('user', 'assistant')
            ORDER BY rank DESC, m.timestamp DESC
            LIMIT $2
            """,
            query,
            limit,
        )
        results = []
        for r in rows:
            text = _plain_text(r["content"])
            if not text:
                continue  # matched only tool-call JSON, nothing readable to show
            results.append(
                {
                    "session_id": str(r["session_id"]),
                    "role": r["role"],
                    "timestamp": r["timestamp"].isoformat() if r["timestamp"] else None,
                    "snippet": _snippet(text, query),
                }
            )
        return json.dumps({"count": len(results), "results": results})
    except (ValueError, TypeError) as e:
        return f"Error: {e}"
