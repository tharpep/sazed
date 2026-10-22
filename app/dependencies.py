"""Shared dependencies for FastAPI routes."""

import hmac

from fastapi import HTTPException, Request

from app.config import settings


def verify_api_key(request: Request) -> None:
    """
    Require a valid API key. Accepts X-API-Key or Authorization: Bearer <key>.
    Auth is off only when API_KEY is empty and ALLOW_NO_AUTH=true (local dev). An empty key
    without that flag refuses every request rather than opening the API; startup refuses
    it too (main.py), this is the second line.
    """
    if not settings.api_key:
        if settings.allow_no_auth:
            return
        raise HTTPException(503, "API_KEY is not configured")

    key: str | None = request.headers.get("X-API-Key")
    if not key:
        auth = request.headers.get("Authorization") or ""
        if auth.lower().startswith("bearer "):
            key = auth[7:].strip()

    if not key or not hmac.compare_digest(key.encode(), settings.api_key.encode()):
        raise HTTPException(401, "Invalid or missing API key")
