"""Application configuration via pydantic-settings."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # App
    debug: bool = False
    allowed_origins: list[str] = ["http://localhost:3000", "http://localhost:3001"]
    # API key (optional): if set, required on all routes except /health
    api_key: str = ""

    # Upstream gateway — single URL, single key for all integrations
    gateway_url: str = ""
    gateway_api_key: str = ""

    # Anthropic
    anthropic_api_key: str = ""
    haiku_model: str = "claude-haiku-4-5-20251001"
    sonnet_model: str = "claude-sonnet-4-5-20250929"

    # Postgres (Cloud SQL or local)
    database_url: str = ""


settings = Settings()
