"""Sazed's client for Pryce's notes, which live behind the gateway's /vault routes."""

import httpx

from app.config import settings
from app.http_client import get_client
from app.notes.filing import Filing

_DETAIL_CHARS = 200


class NotesError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _detail(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        body = None
    if isinstance(body, dict) and isinstance(body.get("detail"), str):
        return body["detail"]
    return resp.text[:_DETAIL_CHARS]


async def _call(method: str, path: str, **kwargs) -> dict:
    client = get_client()
    try:
        resp = await getattr(client, method)(
            f"{settings.gateway_url}/vault{path}",
            headers={"X-API-Key": settings.gateway_api_key},
            timeout=30.0,
            **kwargs,
        )
    except httpx.HTTPError as exc:
        raise NotesError(503, "The notes service could not be reached") from exc
    if not resp.is_success:
        raise NotesError(resp.status_code, _detail(resp))
    return resp.json()


async def list_notes(
    folder: str | None = None, topic: str = "", query: str = "", limit: int = 50, offset: int = 0
) -> dict:
    params: dict[str, str | int] = {"limit": limit, "offset": offset}
    if folder:
        params["folder"] = folder
    if topic:
        params["topic"] = topic
    if query:
        params["q"] = query
    return await _call("get", "/notes", params=params)


async def read_note(path: str) -> dict:
    return await _call("get", "/note", params={"path": path})


async def topics() -> tuple[dict[str, int], int]:
    body = await _call("get", "/topics")
    return {t["name"]: t["count"] for t in body["topics"]}, body["unfiled"]


async def file_under(path: str, filing: Filing) -> dict:
    body = {
        "path": path,
        "topic": filing.topic,
        "reason": filing.reason,
        "kind": filing.kind or "",
        "filer": filing.filer,
    }
    return await _call("post", "/file", json=body)
