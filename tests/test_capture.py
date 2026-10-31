import json
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.capture import service, store


def test_parse_extracted_json_valid():
    raw = '{"journal": null, "tasks": [{"title": "Do laundry"}], "backlog": [], "kb_note": null}'
    parsed = service._parse_extracted_json(raw)
    assert parsed["tasks"] == [{"title": "Do laundry"}]


def test_parse_extracted_json_code_fence():
    raw = '```json\n{"journal": {"title": "Reflection", "body": "Thought"}, "tasks": []}\n```'
    parsed = service._parse_extracted_json(raw)
    assert parsed["journal"]["title"] == "Reflection"


def test_parse_extracted_json_invalid():
    assert service._parse_extracted_json("invalid json") == {}


def test_store_row_deserializes_routed_to():
    row = {
        "id": uuid.uuid4(),
        "content": "Note text",
        "source": "shortcut",
        "status": "processed",
        "routed_to": json.dumps([{"target": "task", "title": "Buy milk"}]),
    }
    result = store._row(row)
    assert isinstance(result["routed_to"], list)
    assert result["routed_to"][0]["title"] == "Buy milk"


@pytest.mark.asyncio
async def test_process_capture_routes_all(monkeypatch):
    cap_id = uuid.uuid4()
    cap_row = {
        "id": cap_id,
        "content": "Meeting with Sarah. Build Sazed tool. Remember key 123.",
        "source": "shortcut",
        "status": "raw",
        "routed_to": [],
    }

    async def fake_get_capture(cid):
        return cap_row

    async def fake_update_capture(capture_id, status, routed_to, error=None):
        return {**cap_row, "status": status, "routed_to": routed_to, "error": error}

    monkeypatch.setattr(store, "get_capture", fake_get_capture)
    monkeypatch.setattr(store, "update_capture", fake_update_capture)

    mock_llm_resp = MagicMock()
    mock_llm_resp.content = [
        MagicMock(
            text=json.dumps(
                {
                    "journal": {
                        "title": "Meeting with Sarah",
                        "body": "Discussed roadmap.",
                        "category": "career",
                    },
                    "tasks": [{"title": "Follow up with Sarah", "notes": ""}],
                    "backlog": [
                        {"title": "Build Sazed tool", "summary": "Tool idea", "why": "Efficiency"}
                    ],
                    "kb_note": {"title": "Key 123", "content": "Details", "category": "notes"},
                }
            )
        )
    ]
    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock(return_value=mock_llm_resp)
    monkeypatch.setattr(service, "get_client", lambda: mock_client)

    mock_http = MagicMock()
    mock_resp = MagicMock(is_success=True)
    mock_resp.json = MagicMock(return_value={"id": "entry-1"})
    mock_http.post = AsyncMock(return_value=mock_resp)
    monkeypatch.setattr(service, "get_http_client", lambda: mock_http)

    async def fake_add_backlog_item(**kwargs):
        return ({"id": uuid.uuid4(), "title": kwargs["title"]}, None)

    monkeypatch.setattr(service.backlog_service, "add_item", fake_add_backlog_item)

    res = await service.process_capture(cap_id)
    assert res["status"] == "processed"
    targets = [r["target"] for r in res["routed_to"]]
    assert "journal" in targets
    assert "task" in targets
    assert "backlog" in targets
    assert "kb" in targets
