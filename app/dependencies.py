"""Shared dependencies for FastAPI routes."""

from fastapi import HTTPException, Request

from app.config import settings


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
