"""Application configuration via pydantic-settings."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # App
    debug: bool = False
    allowed_origins: list[str] = ["https://sazed-frontend.vercel.app", "http://localhost:3000", "http://localhost:3001"]
    # API key (optional): if set, required on all routes except /health
    api_key: str = ""

    # Upstream gateway — single URL, single key for all integrations
    gateway_url: str = ""
    gateway_api_key: str = ""

    # Anthropic
    anthropic_api_key: str = ""
    haiku_model: str = "claude-haiku-4-5-20251001"
    sonnet_model: str = "claude-sonnet-4-6"

    # Postgres (Cloud SQL or local)
    database_url: str = ""

    # KB ingestion
    conversations_folder_id: str = ""   # Drive folder ID for Knowledge Base/Conversations/

    # Feature flags
    session_summarization: bool = False  # Enable agent_memory summarization after each session

    # Context window
    session_window_size: int = 15  # Recent messages to keep verbatim; older messages are compressed


settings = Settings()
