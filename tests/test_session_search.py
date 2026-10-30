import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from app.agent import session_search


def _row(content: str):
    return {
        "session_id": "11111111-1111-1111-1111-111111111111",
        "role": "assistant",
        "content": content,
        "timestamp": datetime(2026, 10, 1, tzinfo=UTC),
        "rank": 0.5,
    }


async def test_returns_snippets_from_text_blocks():
    blocks = json.dumps([{"type": "text", "text": "Restart the nginx container to fix it."}])
    pool = MagicMock(fetch=AsyncMock(return_value=[_row(blocks)]))
    with patch.object(session_search, "get_pool", return_value=pool):
        out = json.loads(await session_search.handle("search_past_sessions", {"query": "nginx"}))
    assert out["count"] == 1
    assert "nginx" in out["results"][0]["snippet"]
    assert out["results"][0]["session_id"].startswith("1111")


async def test_skips_rows_with_no_readable_text():
    blocks = json.dumps([{"type": "tool_use", "name": "x", "input": {"q": "nginx"}}])
    pool = MagicMock(fetch=AsyncMock(return_value=[_row(blocks)]))
    with patch.object(session_search, "get_pool", return_value=pool):
        out = json.loads(await session_search.handle("search_past_sessions", {"query": "nginx"}))
    assert out["count"] == 0


async def test_empty_query_is_an_error():
    assert (await session_search.handle("search_past_sessions", {"query": " "})).startswith("Error")


async def test_limit_is_clamped():
    fetch = AsyncMock(return_value=[])
    with patch.object(session_search, "get_pool", return_value=MagicMock(fetch=fetch)):
        await session_search.handle("search_past_sessions", {"query": "a", "limit": 500})
    assert fetch.call_args.args[2] == 20
