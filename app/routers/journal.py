"""Journal proxy — forwards /journal/* requests to the api-gateway.

Adds one sazed-specific endpoint: POST /journal/sync-kb, which exports markdown
for a period/category and ingests it into the knowledge base.
"""

import logging
import re

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

_INGEST_TIMEOUT = 90.0  # the KB summarizes, chunks and embeds before replying


def _id_part(value: str) -> str:
    """Make a free-form value safe for a KB source_id (letters, digits, '.', '_', '-')."""
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-") or "x"


@router.post("/sync-kb")
async def sync_journal_to_kb(
    category: str = Query(default="career"),
    subcategory: str | None = Query(default=None),
    project: str | None = Query(default=None),
    period: str = Query(default="week"),
):
    return await sync_period_to_kb(category, subcategory or project, period)


async def sync_period_to_kb(category: str, sub: str | None, period: str) -> dict:
    """Export journal entries for a period and ingest them into the knowledge base.

    Each (category, subcategory, date range) gets a stable KB source_id, so syncing the same
    week again replaces that week's copy instead of adding a duplicate. Also backs the
    sync_journal_to_kb agent tool. Raises HTTPException on gateway or KB failures.
    """
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

    # 2. Fetch summary to get the date range for the title and source id
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

    label = f"{category}/{sub}" if sub else category
    span = f"{start} to {end}" if end else start
    # .md so the KB chunks it by heading
    title = f"Journal {label} {span}.md"
    source_id = "-".join(
        _id_part(bit) for bit in ["journal", category, sub or "all", start, end or "open"]
    )

    # 3. Ingest straight into the KB (replaces any earlier copy with the same source_id)
    ingest_resp = await client.post(
        f"{base}/kb/ingest/text",
        json={"title": title, "content": content, "category": "journal", "source_id": source_id},
        headers=headers,
        timeout=_INGEST_TIMEOUT,
    )
    if not ingest_resp.is_success:
        msg = f"KB ingest failed ({ingest_resp.status_code}): {ingest_resp.text}"
        logger.error(msg)
        raise HTTPException(502, msg)

    result = ingest_resp.json()
    logger.info(f"Journal synced to KB: {title} ({result.get('chunk_count')} chunks)")
    return {
        "synced": True,
        "title": title,
        "file_id": result.get("file_id"),
        "chunk_count": result.get("chunk_count"),
        "category": category,
        "subcategory": sub,
        "period": period,
    }
