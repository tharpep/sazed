"""Unit tests for tool read cache and write invalidation (app/agent/tools.py)."""

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.agent.tools import (
    _TOOL_CACHE,
    clear_tool_cache,
    execute_tool,
    invalidate_tool_cache,
)


@pytest.fixture(autouse=True)
def _setup_cache(monkeypatch):
    clear_tool_cache()
    monkeypatch.setattr("app.agent.tools.get_client", lambda: AsyncMock())
    yield
    clear_tool_cache()


def _mock_response(
    status_code: int = 200,
    json_data: dict | list | None = None,
    text: str = "",
) -> httpx.Response:
    request = httpx.Request("GET", "https://gateway.local/test")
    if json_data is not None:
        return httpx.Response(status_code=status_code, json=json_data, request=request)
    return httpx.Response(status_code=status_code, text=text, request=request)


@pytest.mark.asyncio
async def test_create_event_invalidates_get_events_cache():
    initial_events = [{"id": "ev-1", "summary": "Initial Event"}]
    updated_events = [
        {"id": "ev-1", "summary": "Initial Event"},
        {"id": "ev-2", "summary": "New Event"},
    ]

    with patch("app.agent.tools._send_gateway_request", new_callable=AsyncMock) as mock_send:
        mock_send.return_value = _mock_response(200, initial_events)

        res1 = await execute_tool("get_events", {"time_min": "2026-11-14T00:00:00Z"})
        assert res1.status == "success"
        assert "Initial Event" in res1.content
        assert mock_send.call_count == 1

        res2 = await execute_tool("get_events", {"time_min": "2026-11-14T00:00:00Z"})
        assert res2.status == "success"
        assert res2.duration_ms == 0
        assert mock_send.call_count == 1

        mock_send.return_value = _mock_response(200, {"id": "ev-2", "summary": "New Event"})
        write_args = {"summary": "New Event", "start": "2026-11-14T10:00:00Z"}
        write_res = await execute_tool("create_event", write_args)
        assert write_res.status == "success"
        assert mock_send.call_count == 2

        mock_send.return_value = _mock_response(200, updated_events)
        res3 = await execute_tool("get_events", {"time_min": "2026-11-14T00:00:00Z"})
        assert res3.status == "success"
        assert "New Event" in res3.content
        assert mock_send.call_count == 3


@pytest.mark.asyncio
async def test_category_invalidation_isolation():
    with patch("app.agent.tools._send_gateway_request", new_callable=AsyncMock) as mock_send:
        mock_send.side_effect = [
            _mock_response(200, [{"id": "ev-1"}]),
            _mock_response(200, [{"id": "task-1"}]),
            _mock_response(200, {"id": "task-2"}),
            _mock_response(200, [{"id": "task-1"}, {"id": "task-2"}]),
        ]

        await execute_tool("get_events", {})
        await execute_tool("get_tasks", {"list_id": "l-1"})
        assert mock_send.call_count == 2

        await execute_tool("create_task", {"list_id": "l-1", "title": "Task 2"})
        assert mock_send.call_count == 3

        events_res = await execute_tool("get_events", {})
        assert events_res.duration_ms == 0
        assert mock_send.call_count == 3

        tasks_res = await execute_tool("get_tasks", {"list_id": "l-1"})
        assert tasks_res.status == "success"
        assert mock_send.call_count == 4


@pytest.mark.asyncio
async def test_delete_204_invalidates_cache():
    with patch("app.agent.tools._send_gateway_request", new_callable=AsyncMock) as mock_send:
        mock_send.side_effect = [
            _mock_response(200, [{"id": "f-1", "name": "doc.txt"}]),
            _mock_response(204),
            _mock_response(200, []),
        ]

        await execute_tool("list_files", {})
        assert mock_send.call_count == 1

        delete_res = await execute_tool("delete_file", {"file_id": "f-1"})
        assert delete_res.status == "success"
        assert mock_send.call_count == 2

        after_delete = await execute_tool("list_files", {})
        assert after_delete.status == "success"
        assert mock_send.call_count == 3


@pytest.mark.asyncio
async def test_failed_write_does_not_invalidate_cache():
    with patch("app.agent.tools._send_gateway_request", new_callable=AsyncMock) as mock_send:
        mock_send.side_effect = [
            _mock_response(200, [{"id": "ev-1"}]),
            _mock_response(500, text="Internal Server Error"),
        ]

        await execute_tool("get_events", {})
        assert mock_send.call_count == 1

        failed_write = await execute_tool("create_event", {"summary": "Broken"})
        assert failed_write.status == "error"
        assert mock_send.call_count == 2

        cached_res = await execute_tool("get_events", {})
        assert cached_res.duration_ms == 0
        assert mock_send.call_count == 2


def test_invalidate_tool_cache_helper():
    _TOOL_CACHE[("get_events", (("time_min", "1"),))] = "cached_events"
    _TOOL_CACHE[("get_tasks", ())] = "cached_tasks"

    invalidate_tool_cache("calendar")
    assert ("get_events", (("time_min", "1"),)) not in _TOOL_CACHE
    assert ("get_tasks", ()) in _TOOL_CACHE

    clear_tool_cache()
    assert len(_TOOL_CACHE) == 0
