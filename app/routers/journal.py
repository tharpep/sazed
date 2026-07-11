"""Journal proxy — forwards /journal/* requests to the api-gateway.

Adds one sazed-specific endpoint: POST /journal/sync-kb, which exports markdown
for a period/category and pushes it through Drive into the knowledge base.
"""

import logging

import httpx
from fastapi import APIRouter, HTTPException, Query, Request, Response

from app.config import settings
from app.http_client import get_client

logger = logging.getLogger(__name__)

router = APIRouter(tags=["journal"])

_TIMEOUT = 30.0


def _gateway_url(path: str) -> str:
    return f"{settings.gateway_url.rstrip('/')}{path}"


def _headers() -> dict[str, str]:
    return {"X-API-Key": settings.gateway_api_key}


async def _proxy(method: str, path: str, **kwargs) -> Response:
    if not settings.gateway_url:
        raise HTTPException(503, "Gateway URL not configured")
    try:
        resp = await get_client().request(
            method, _gateway_url(path), headers=_headers(), timeout=_TIMEOUT, **kwargs
        )
    except httpx.TimeoutException:
        raise HTTPException(504, "Gateway timed out")
    except httpx.RequestError as e:
        raise HTTPException(502, f"Gateway unreachable: {e}")

    return Response(
        content=resp.content,
        status_code=resp.status_code,
        media_type=resp.headers.get("content-type", "application/json"),
    )


def _drop_none(d: dict) -> dict:
    return {k: v for k, v in d.items() if v is not None}


# ── Entries ───────────────────────────────────────────────────────────────


@router.get("/entries")
async def list_entries(
    category: str | None = Query(default=None),
    subcategory: str | None = Query(default=None),
    project: str | None = Query(default=None),
    tag: str | None = Query(default=None),
    q: str | None = Query(default=None),
    start_date: str | None = Query(default=None),
    end_date: str | None = Query(default=None),
    limit: int = Query(default=30),
    cursor: str | None = Query(default=None),
):
    params = _drop_none({
        "category": category, "subcategory": subcategory, "project": project,
        "tag": tag, "q": q,
        "start_date": start_date, "end_date": end_date,
        "limit": limit, "cursor": cursor,
    })
    return await _proxy("GET", "/journal/entries", params=params)


@router.get("/entries/{entry_id}")
async def get_entry(entry_id: str):
    return await _proxy("GET", f"/journal/entries/{entry_id}")


@router.post("/entries")
async def create_entry(request: Request):
    return await _proxy("POST", "/journal/entries", json=await request.json())


@router.patch("/entries/{entry_id}")
async def update_entry(entry_id: str, request: Request):
    return await _proxy("PATCH", f"/journal/entries/{entry_id}", json=await request.json())


@router.delete("/entries/{entry_id}")
async def delete_entry(entry_id: str):
    return await _proxy("DELETE", f"/journal/entries/{entry_id}")


# ── Taxonomy ─────────────────────────────────────────────────────────────


@router.get("/categories")
async def list_categories():
    return await _proxy("GET", "/journal/categories")


@router.get("/subcategories")
async def list_subcategories(category: str | None = Query(default=None)):
    params = _drop_none({"category": category})
    return await _proxy("GET", "/journal/subcategories", params=params)


@router.get("/projects")  # deprecated alias kept for one release
async def list_projects():
    return await _proxy("GET", "/journal/projects")


# ── Summary ──────────────────────────────────────────────────────────────


@router.get("/summary")
async def journal_summary(
    category: str | None = Query(default=None),
    subcategory: str | None = Query(default=None),
    project: str | None = Query(default=None),
    period: str | None = Query(default=None),
    start_date: str | None = Query(default=None),
    end_date: str | None = Query(default=None),
):
    params = _drop_none({
        "category": category, "subcategory": subcategory, "project": project,
        "period": period,
        "start_date": start_date, "end_date": end_date,
    })
    return await _proxy("GET", "/journal/summary", params=params)


# ── Export ────────────────────────────────────────────────────────────────


@router.get("/export")
async def export_entries(
    category: str | None = Query(default=None),
    subcategory: str | None = Query(default=None),
    project: str | None = Query(default=None),
    period: str | None = Query(default=None),
    start_date: str | None = Query(default=None),
    end_date: str | None = Query(default=None),
    format: str = Query(default="markdown"),
):
    params = _drop_none({
        "category": category, "subcategory": subcategory, "project": project,
        "period": period,
        "start_date": start_date, "end_date": end_date,
        "format": format,
    })
    return await _proxy("GET", "/journal/export", params=params)


# ── KB Sync ──────────────────────────────────────────────────────────────


def _folder_id_for(category: str) -> str:
    """Return the configured Drive folder id for a category, or '' if unset."""
    if category == "personal":
        return settings.personal_journal_folder_id
    return settings.journal_folder_id  # career (default)


@router.post("/sync-kb")
async def sync_journal_to_kb(
    category: str = Query(default="career"),
    subcategory: str | None = Query(default=None),
    project: str | None = Query(default=None),
    period: str = Query(default="week"),
):
    """Export journal entries for a period and ingest into the knowledge base via Drive."""
    folder_id = _folder_id_for(category)
    if not folder_id:
        raise HTTPException(
            503,
            f"No Drive folder configured for category '{category}'. "
            "Set the corresponding *_journal_folder_id in app/config.py.",
        )

    sub = subcategory or project
    base = settings.gateway_url.rstrip("/")
    headers = _headers()
    client = get_client()

    # 1. Fetch the markdown export from the gateway
    export_params: dict = {
        "category": category,
        "period": period,
        "format": "markdown",
    }
    if sub:
        export_params["subcategory"] = sub
    export_resp = await client.get(
        f"{base}/journal/export", params=export_params, headers=headers, timeout=_TIMEOUT,
    )
    if not export_resp.is_success:
        raise HTTPException(502, f"Export fetch failed: {export_resp.text}")

    content = export_resp.text
    if not content or content == "No journal entries found for the given range.":
        return {"synced": False, "reason": "No entries for the given period."}

    # 2. Fetch summary to get the date range for the filename
    summary_params: dict = {"category": category, "period": period}
    if sub:
        summary_params["subcategory"] = sub
    summary_resp = await client.get(
        f"{base}/journal/summary", params=summary_params, headers=headers, timeout=_TIMEOUT,
    )
    start = period
    end = ""
    if summary_resp.is_success:
        s = summary_resp.json()
        start = s.get("start_date", period)
        end = s.get("end_date", "")

    slug_bits = [category]
    if sub:
        slug_bits.append(sub)
    slug = "-".join(slug_bits)
    filename = f"journal-{slug}-{start}-to-{end}.md"

    # 3. Write to Drive
    write_resp = await client.post(
        f"{base}/storage/files",
        json={
            "name": filename,
            "content": content,
            "folder_id": folder_id,
            "mime_type": "text/plain",
        },
        headers=headers,
        timeout=_TIMEOUT,
    )
    if not write_resp.is_success:
        msg = f"Drive upload failed ({write_resp.status_code}): {write_resp.text}"
        logger.error(msg)
        raise HTTPException(502, msg)

    logger.info(f"Journal exported to Drive: {filename}")

    # 4. Trigger KB sync
    sync_resp = await client.post(f"{base}/kb/sync", headers=headers, timeout=_TIMEOUT)
    if not sync_resp.is_success:
        logger.warning(f"KB sync trigger failed: {sync_resp.status_code}")

    return {
        "synced": True,
        "filename": filename,
        "category": category,
        "subcategory": sub,
        "period": period,
    }
