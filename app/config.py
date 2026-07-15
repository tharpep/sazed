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
    # DEPRECATED: no longer read anywhere. Session-summary KB ingestion now goes
    # through a direct /kb/ingest/text call (see session_kb_ingest_enabled below),
    # not a Drive-write-then-sync round-trip — kept here, not deleted, since it's
    # still a real Drive folder ID that may be referenced by other tooling.
    conversations_folder_id: str = "109Nh8yA11PpQ4iWbJ6LHGIL-2roCn5Ok"  # Drive folder ID for Knowledge Base/Conversations/
    # KB/Journal/Career/ — used by /journal/sync-kb when category=career
    journal_folder_id: str = "1mZrbPlHW0TsP7oNDpzSSi38Hjt-PfAVU"
    # TODO: paste KB/Journal/Personal/ Drive folder ID when the folder is created
    personal_journal_folder_id: str = ""

    # Feature flags
    session_summarization: bool = True  # Generate agent_memory summary after each session
    session_kb_ingest_enabled: bool = True  # Ingest each session's structured summary into the KB

    # Context window
    session_window_size: int = 15  # Recent messages to keep verbatim; older messages are compressed

    # Memory
    memory_facts_per_type_limit: int = 15  # Max facts per fact_type injected into the prompt

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
        "append_to_file", "move_file", "copy_file", "copy_file_from_github",
        # Sheets
        "create_spreadsheet", "write_sheet", "append_sheet_rows", "clear_sheet_range",
        # GitHub
        "create_issue", "update_issue", "create_pr", "add_issue_comment", "add_pr_comment",
        # Knowledge base — mutates the index the agent itself retrieves from
        "delete_kb_source", "sync_kb", "ingest_text", "ingest_url",
        # Finance — precision matters for figures/dates
        "add_subscription", "update_subscription", "delete_subscription",
        "set_budget_limit", "delete_budget", "add_income_source", "delete_income",
        # Journal — precision matters for dates/contributions
        "create_journal_entry", "update_journal_entry", "delete_journal_entry",
        "sync_journal_to_kb",
    ]

    # Security: prompt-injection hardening
    confirmation_required: bool = True
    sensitive_tools: list[str] = [
        "draft_email", "delete_event", "delete_task", "delete_file",
        "delete_kb_source", "delete_journal_entry", "delete_subscription",
        "delete_income", "delete_budget",
    ]
    untrusted_content_tools: list[str] = [
        "get_email", "list_emails", "search_emails", "read_file", "fetch_url",
        "web_search", "aggregate_search", "search_knowledge_base", "get_kb_index",
        "read_kb_source", "get_github_file", "search_code", "search_places",
    ]
    email_recipient_allowlist: list[str] = []  # empty = allow all

    # Procedural memory
    procedural_memory_enabled: bool = True
    procedure_min_write_tools: int = 2   # min write tools in a session to propose a procedure
    max_procedures_in_prompt: int = 3

    # Observability — LLM cost/token tracking
    llm_cost_tracking: bool = True

    # Observability — error tracking (Sentry). Empty DSN = fully disabled, no-op;
    # nothing to configure unless/until a Sentry project exists.
    sentry_dsn: str = ""
    sentry_traces_sample_rate: float = 0.1

    # Reflection loop
    reflection_enabled: bool = True
    max_lessons_per_session: int = 2


settings = Settings()
