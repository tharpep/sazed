"""KB proxy — forwards /kb/* requests to the api-gateway."""

import httpx
from fastapi import APIRouter, HTTPException, Query, Request, Response

from app.config import settings

router = APIRouter(tags=["kb"])

_TIMEOUT = 60.0  # sync can take a while


def _gateway_url(path: str) -> str:
    return f"{settings.gateway_url.rstrip('/')}{path}"


def _headers() -> dict[str, str]:
    return {"X-API-Key": settings.gateway_api_key}


async def _proxy(method: str, path: str, **kwargs) -> Response:
    """Forward a request to the api-gateway and return the raw response."""
    if not settings.gateway_url:
        raise HTTPException(503, "Gateway URL not configured")
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.request(
                method, _gateway_url(path), headers=_headers(), **kwargs
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


# ---------------------------------------------------------------------------
# Stats + file listings
# ---------------------------------------------------------------------------


@router.get("/stats")
async def kb_stats():
    return await _proxy("GET", "/kb/stats")


@router.get("/sources")
async def kb_sources():
    return await _proxy("GET", "/kb/sources")


@router.get("/files")
async def kb_files():
    return await _proxy("GET", "/kb/files")


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


@router.post("/search")
async def kb_search(request: Request):
    body = await request.json()
    return await _proxy("POST", "/kb/search", json=body)


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------


@router.post("/sync")
async def kb_sync(force: bool = Query(False)):
    return await _proxy("POST", "/kb/sync", params={"force": force})


# ---------------------------------------------------------------------------
# Deletions
# ---------------------------------------------------------------------------


@router.delete("/files/{drive_file_id}")
async def kb_delete_file(drive_file_id: str):
    return await _proxy("DELETE", f"/kb/files/{drive_file_id}")


@router.delete("")
async def kb_clear():
    return await _proxy("DELETE", "/kb")
