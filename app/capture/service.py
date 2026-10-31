import json
import logging
import uuid
from typing import Any

from app.agent.client import get_client, model_params
from app.agent.json_utils import strip_json_fence
from app.backlog import service as backlog_service
from app.capture import store
from app.config import settings
from app.http_client import get_client as get_http_client

logger = logging.getLogger(__name__)

_CLASSIFY_PROMPT = """You are triaging a raw capture (voice dump, quick thought, or note).
Analyze the input text and extract all appropriate structured outputs.
A single capture may produce multiple outputs (e.g. both a journal reflection and a task).

Classify into any applicable categories:
1. "journal": Personal reflections, feelings, daily worklogs, or career updates.
   Provide: {"title": str, "category": "career" | "personal", "body": str}
2. "tasks": Concrete actionable to-dos or things to follow up on.
   Provide array: [{"title": str, "notes": str}]
3. "backlog": Ideas for personal projects, Sazed features, homelab improvements, or fixes.
   Provide array: [{"title": str, "summary": str, "why": str, "effort": "S"|"M"|"L",
                    "impact": "low"|"medium"|"high"}]
4. "kb_note": Reference notes, durable facts, or informational write-ups for the KB.
   Provide: {"title": str, "content": str, "category": str}

Output pure JSON adhering to this schema:
{
  "journal": null,
  "tasks": [],
  "backlog": [],
  "kb_note": null
}
If the text does not fit a category, leave null/empty.
If it is only a general thought, at minimum produce a kb_note.
"""


def _parse_extracted_json(text: str) -> dict[str, Any]:
    try:
        data = json.loads(strip_json_fence(text))
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


async def ingest_capture(content: str, source: str = "shortcut") -> dict[str, Any]:
    capture = await store.create_capture(content=content, source=source)
    return await process_capture(uuid.UUID(str(capture["id"])))


async def process_capture(capture_id: uuid.UUID) -> dict[str, Any]:
    capture = await store.get_capture(capture_id)
    if not capture:
        raise ValueError(f"Capture not found: {capture_id}")

    prompt = f"{_CLASSIFY_PROMPT}\n\nCapture text:\n{capture['content']}"
    response = await get_client().messages.create(
        model=settings.haiku_model,
        **model_params(settings.haiku_model),
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    raw_text = response.content[0].text if response.content else ""
    parsed = _parse_extracted_json(raw_text)

    base = settings.gateway_url.rstrip("/")
    headers = {"X-API-Key": settings.gateway_api_key} if settings.gateway_api_key else {}
    http_client = get_http_client()

    routed_to: list[dict[str, Any]] = []

    journal = parsed.get("journal")
    if isinstance(journal, dict) and journal.get("title") and journal.get("body"):
        try:
            resp = await http_client.post(
                f"{base}/journal/entries",
                json={
                    "title": journal["title"],
                    "body": journal["body"],
                    "category": journal.get("category", "personal"),
                },
                headers=headers,
                timeout=20.0,
            )
            if resp.is_success:
                entry = resp.json()
                routed_to.append(
                    {
                        "target": "journal",
                        "title": journal["title"],
                        "id": entry.get("id"),
                        "status": "created",
                    }
                )
            else:
                routed_to.append(
                    {
                        "target": "journal",
                        "title": journal["title"],
                        "status": f"failed: {resp.status_code}",
                    }
                )
        except Exception as e:
            logger.warning("Failed to route capture to journal: %s", e)
            routed_to.append(
                {
                    "target": "journal",
                    "title": journal["title"],
                    "status": f"error: {e}",
                }
            )

    for t in parsed.get("tasks") or []:
        if isinstance(t, dict) and t.get("title"):
            try:
                resp = await http_client.post(
                    f"{base}/tasks/lists/@default/tasks",
                    json={"title": t["title"], "notes": t.get("notes", "")},
                    headers=headers,
                    timeout=20.0,
                )
                if resp.is_success:
                    routed_to.append({"target": "task", "title": t["title"], "status": "created"})
                else:
                    routed_to.append(
                        {
                            "target": "task",
                            "title": t["title"],
                            "status": f"failed: {resp.status_code}",
                        }
                    )
            except Exception as e:
                logger.warning("Failed to route capture to tasks: %s", e)
                routed_to.append({"target": "task", "title": t["title"], "status": f"error: {e}"})

    for b in parsed.get("backlog") or []:
        if isinstance(b, dict) and b.get("title"):
            try:
                new_item, dup = await backlog_service.add_item(
                    type="idea",
                    title=b["title"],
                    source="capture",
                    summary=b.get("summary", ""),
                    why=b.get("why", ""),
                    effort=b.get("effort", "S"),
                    impact=b.get("impact", "medium"),
                )
                status = "created" if new_item else "duplicate"
                item_id = str(new_item["id"]) if new_item else str(dup["id"]) if dup else None
                routed_to.append(
                    {
                        "target": "backlog",
                        "title": b["title"],
                        "id": item_id,
                        "status": status,
                    }
                )
            except Exception as e:
                logger.warning("Failed to route capture to backlog: %s", e)
                routed_to.append(
                    {
                        "target": "backlog",
                        "title": b["title"],
                        "status": f"error: {e}",
                    }
                )

    kb_note = parsed.get("kb_note")
    if isinstance(kb_note, dict) and kb_note.get("title") and kb_note.get("content"):
        try:
            resp = await http_client.post(
                f"{base}/kb/ingest/text",
                json={
                    "title": kb_note["title"],
                    "content": kb_note["content"],
                    "category": kb_note.get("category", "capture"),
                },
                headers=headers,
                timeout=30.0,
            )
            if resp.is_success:
                routed_to.append({"target": "kb", "title": kb_note["title"], "status": "indexed"})
            else:
                routed_to.append(
                    {
                        "target": "kb",
                        "title": kb_note["title"],
                        "status": f"failed: {resp.status_code}",
                    }
                )
        except Exception as e:
            logger.warning("Failed to route capture to KB: %s", e)
            routed_to.append({"target": "kb", "title": kb_note["title"], "status": f"error: {e}"})

    status = "processed" if routed_to else "unclassified"
    updated = await store.update_capture(capture_id=capture_id, status=status, routed_to=routed_to)
    return updated or capture
