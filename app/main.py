"""Sazed — personal AI agent entry point."""

import logging
from contextlib import asynccontextmanager

import sentry_sdk
from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.logging import LoggingIntegration

from app.config import settings
from app.db import close_pool, init_pool
from app.dependencies import verify_api_key
from app.http_client import shutdown as http_client_shutdown
from app.http_client import startup as http_client_startup
from app.routers import (
    audit,
    chat,
    conversations,
    finance,
    health,
    journal,
    kb,
    memory,
    procedures,
    think,
    tools,
)

logger = logging.getLogger(__name__)


def _configure_logging() -> None:
    level = logging.DEBUG if settings.debug else logging.INFO
    logging.basicConfig(level=level, force=True)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    if settings.debug:
        logger.debug("DEBUG logging enabled — full agent flow output active")


def _configure_sentry() -> None:
    """No-op unless SENTRY_DSN is set — nothing to configure until a Sentry
    project exists. logger.error/exception calls are captured automatically
    via the logging integration once it is."""
    if not settings.sentry_dsn:
        return
    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        integrations=[
            FastApiIntegration(),
            LoggingIntegration(level=logging.INFO, event_level=logging.ERROR),
        ],
        traces_sample_rate=settings.sentry_traces_sample_rate,
        send_default_pii=False,
    )
    logger.info("Sentry error tracking enabled")


@asynccontextmanager
async def lifespan(app: FastAPI):
    _configure_logging()
    _configure_sentry()
    await init_pool()
    await http_client_startup()
    yield
    await http_client_shutdown()
    await close_pool()


app = FastAPI(
    title="Sazed",
    description="Personal AI agent",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Health is public; all other routes require API key when API_KEY is set
app.include_router(health.router)
app.include_router(
    chat.router, prefix="/chat", tags=["chat"], dependencies=[Depends(verify_api_key)]
)
app.include_router(
    conversations.router,
    prefix="/conversations",
    tags=["conversations"],
    dependencies=[Depends(verify_api_key)],
)
app.include_router(
    memory.router, prefix="/memory", tags=["memory"], dependencies=[Depends(verify_api_key)]
)
app.include_router(
    kb.router, prefix="/kb", tags=["kb"], dependencies=[Depends(verify_api_key)]
)
app.include_router(
    tools.router, prefix="/tools", tags=["tools"], dependencies=[Depends(verify_api_key)]
)
app.include_router(
    audit.router, prefix="/audit", tags=["audit"], dependencies=[Depends(verify_api_key)]
)
app.include_router(
    finance.router, prefix="/finance", tags=["finance"], dependencies=[Depends(verify_api_key)]
)
app.include_router(
    journal.router, prefix="/journal", tags=["journal"], dependencies=[Depends(verify_api_key)]
)
app.include_router(
    think.router, prefix="/think", tags=["think"], dependencies=[Depends(verify_api_key)]
)
app.include_router(
    procedures.router,
    prefix="/procedures",
    tags=["procedures"],
    dependencies=[Depends(verify_api_key)],
)
