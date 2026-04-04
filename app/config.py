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
    conversations_folder_id: str = "109Nh8yA11PpQ4iWbJ6LHGIL-2roCn5Ok"  # Drive folder ID for Knowledge Base/Conversations/

    # Feature flags
    session_summarization: bool = True  # Generate agent_memory summary after each session

    # Context window
    session_window_size: int = 15  # Recent messages to keep verbatim; older messages are compressed

    # Agent loop
    agent_max_turns: int = 20  # Maximum tool-call turns per request (AGENT_MAX_TURNS in .env)
    turn_timeout_seconds: int = 300  # Max seconds per LLM call before timing out (TURN_TIMEOUT_SECONDS in .env)

    # Tool result cache
    tool_cache_ttl_seconds: int = 60  # TTL for read-only tool result cache (TOOL_CACHE_TTL_SECONDS in .env)

    # Model routing — Haiku by default, escalate to Sonnet based on these signals
    sonnet_turn_threshold: int = 2          # Turn index at which all remaining turns use Sonnet
    sonnet_message_len_threshold: int = 500  # User message char count that signals Sonnet on turn 0
    sonnet_write_tools: list[str] = [        # Any of these in prior turns forces Sonnet for all subsequent turns
        # Calendar
        "create_event", "update_event", "delete_event",
        # Tasks
        "create_task", "update_task", "delete_task", "create_task_list", "rename_task_list",
        # Email
        "draft_email",
        # Drive
        "create_file", "update_file", "delete_file", "create_folder",
        # Sheets
        "create_spreadsheet", "write_sheet",
        # GitHub
        "create_issue", "update_issue", "create_pr",
    ]


settings = Settings()
