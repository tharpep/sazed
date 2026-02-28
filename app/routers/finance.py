"""Finance proxy — forwards /finance/* requests to the api-gateway."""

import httpx
from fastapi import APIRouter, HTTPException, Query, Request, Response

from app.config import settings

router = APIRouter(tags=["finance"])

_TIMEOUT = 30.0


def _gateway_url(path: str) -> str:
    return f"{settings.gateway_url.rstrip('/')}{path}"


def _headers() -> dict[str, str]:
    return {"X-API-Key": settings.gateway_api_key}


async def _proxy(method: str, path: str, **kwargs) -> Response:
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


# ── Subscriptions ──────────────────────────────────────────────────────────


@router.get("/subscriptions")
async def list_subscriptions(include_inactive: bool = Query(default=False, alias="all")):
    return await _proxy("GET", "/finance/subscriptions", params={"all": include_inactive})


@router.post("/subscriptions")
async def create_subscription(request: Request):
    return await _proxy("POST", "/finance/subscriptions", json=await request.json())


@router.patch("/subscriptions/{sub_id}")
async def update_subscription(sub_id: str, request: Request):
    return await _proxy("PATCH", f"/finance/subscriptions/{sub_id}", json=await request.json())


@router.delete("/subscriptions/{sub_id}")
async def delete_subscription(sub_id: str):
    return await _proxy("DELETE", f"/finance/subscriptions/{sub_id}")


# ── Budget ─────────────────────────────────────────────────────────────────


@router.get("/budget")
async def list_budget():
    return await _proxy("GET", "/finance/budget")


@router.put("/budget/{category}")
async def upsert_budget(category: str, request: Request):
    return await _proxy("PUT", f"/finance/budget/{category}", json=await request.json())


@router.delete("/budget/{category}")
async def delete_budget(category: str):
    return await _proxy("DELETE", f"/finance/budget/{category}")


# ── Income ─────────────────────────────────────────────────────────────────


@router.get("/income")
async def list_income():
    return await _proxy("GET", "/finance/income")


@router.post("/income")
async def create_income(request: Request):
    return await _proxy("POST", "/finance/income", json=await request.json())


@router.patch("/income/{income_id}")
async def update_income(income_id: str, request: Request):
    return await _proxy("PATCH", f"/finance/income/{income_id}", json=await request.json())


@router.delete("/income/{income_id}")
async def delete_income(income_id: str):
    return await _proxy("DELETE", f"/finance/income/{income_id}")


# ── Upcoming bills ──────────────────────────────────────────────────────────


@router.get("/upcoming")
async def upcoming_bills(days: int = Query(default=30)):
    return await _proxy("GET", "/finance/upcoming", params={"days": days})


# ── Summary ────────────────────────────────────────────────────────────────


@router.get("/summary")
async def monthly_summary():
    return await _proxy("GET", "/finance/summary")
