"""Shared dependencies for FastAPI routes."""

from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request

from app.config import settings


def _shape_v2(x_sazed_shape: Annotated[str | None, Header()] = None) -> bool:
    return x_sazed_shape == "2"


# One stack seam 1: dispatch and profile routes answer in the pre-035 shape unless the caller
# sends X-Sazed-Shape: 2, until the worker and console have moved to the new one.
ShapeV2 = Annotated[bool, Depends(_shape_v2)]


def verify_api_key(request: Request) -> None:
    """
    Require a valid API key when settings.api_key is set.
    Accepts X-API-Key or Authorization: Bearer <key>.
    Auth is disabled when API_KEY env var is empty (local dev mode).
    """
    if not settings.api_key:
        return

    key: str | None = request.headers.get("X-API-Key")
    if not key:
        auth = request.headers.get("Authorization") or ""
        if auth.lower().startswith("bearer "):
            key = auth[7:].strip()

    if not key or key != settings.api_key:
        raise HTTPException(401, "Invalid or missing API key")
