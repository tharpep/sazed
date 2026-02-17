"""Sazed — personal AI agent entry point."""

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.db import close_pool, init_pool
from app.dependencies import verify_api_key
from app.routers import chat, conversations, health, memory


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_pool()
    yield
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
