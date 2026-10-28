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
    # Automations v2 scheduler (app/automations/runner.py); off = rows stay but nothing fires
    automations_scheduler: bool = True
    # Pushover when an automation run errors or stops at an approval gate, regardless of
    # its outputs (app/automations/runner.py:_alert).
    automation_alerts: bool = True

    # Worker jobs (plans/07, app/jobs/). The sazed-worker container authenticates with
    # X-Worker-Key; empty disables the worker API (as on GCP).
    worker_key: str = ""
    # Models the worker may run, per agent (allowlist: anything else is refused, no Fable).
    agy_models: list[str] = [
        "gemini-3.8-flash-high",
        "gemini-3.8-flash-medium",
        "gemini-3.8-flash-low",
        "claude-sonnet-4-6",
        "claude-opus-4-6-thinking",
        "gpt-oss-120b-medium",
    ]
    claude_models: list[str] = ["claude-sonnet-5-5", "claude-haiku-5-5", "claude-opus-5-5"]
    # Jobs per agent per rolling 24h; protects the subscriptions for Pryce's own use (Claude Pro).
    jobs_daily_cap_agy: int = 20
    jobs_daily_cap_claude: int = 5
    research_wait_seconds: int = 60  # how long the research tool waits before handing back a job id
    # Repos Sazed never runs code tasks on (dormant; the gateway's bot refuses them too).
    code_task_excluded_repos: list[str] = [
        "tharpep/Trusted-Model-Registry",
        "tharpep/gotenberg-fork",
    ]

    # Scout and triage agents (app/agents/scout.py, app/backlog/pipelines.py; plans/07 9b, 12)
    console_url: str = "https://sazed.tail77c590.ts.net"  # link target in Pushover messages
    # Repos the scout thinks about and approved ideas may target
    scout_repos: list[str] = [
        "tharpep/sazed",
        "tharpep/api-gateway",
        "tharpep/knowledge-base",
        "tharpep/sazed-frontend",
        "tharpep/sazed-worker",
        "tharpep/sazed-mcp",
    ]
    # Generates on Gemini; filters and ranks on a different family so one model's blind spots
    # don't decide both steps.
    scout_research_model: str = "gemini-3.8-flash-high"
    scout_judge_model: str = "claude-opus-4-6-thinking"
    scout_max_ideas: int = 3
    backlog_stale_days: int = 30

    # Public architecture map for the portfolio (app/activity/public_map.py): Upstash Redis REST
    # URL and a write token. Empty = publisher off.
    public_map_redis_url: str = ""
    public_map_redis_token: str = ""
    public_map_key: str = "homelab:map"

    # Per-model request settings (thinking, effort) live in agent/client.py:model_params
    # Routing (plans/11 phase 1): Sonnet is the front door for every chat turn, Opus takes a
    # turn over on request_escalation and runs the think loop, Haiku does background calls.
    haiku_model: str = "claude-haiku-5-5"
    sonnet_model: str = "claude-sonnet-5-5"
    opus_model: str = "claude-opus-5-5"

    # Postgres (Cloud SQL or local)
    database_url: str = ""

    # KB ingestion
    # DEPRECATED: no longer read anywhere. Session-summary KB ingestion now goes
    # through a direct /kb/ingest/text call (see session_kb_ingest_enabled below),
    # not a Drive-write-then-sync round-trip — kept here, not deleted, since it's
    # still a real Drive folder ID that may be referenced by other tooling.
    conversations_folder_id: str = "109Nh8yA11PpQ4iWbJ6LHGIL-2roCn5Ok"  # Drive folder ID for Knowledge Base/Conversations/

    # Feature flags
    session_summarization: bool = True  # Generate agent_memory summary after each session
    session_kb_ingest_enabled: bool = True  # Ingest each session's structured summary into the KB
    idle_session_hours: int = 2  # Process a chat session (memory, KB) once idle this long

    # Context window
    session_window_size: int = 15  # Recent messages to keep verbatim; older messages are compressed

    # Memory
    memory_facts_per_type_limit: int = 15  # Max facts per fact_type injected into the prompt

    # Agent loop
    agent_max_turns: int = 20  # Maximum tool-call turns per request (AGENT_MAX_TURNS in .env)
    turn_timeout_seconds: int = 300  # Max seconds per LLM call before timing out (TURN_TIMEOUT_SECONDS in .env)

    # Tool result cache
    tool_cache_ttl_seconds: int = 60  # TTL for read-only tool result cache (TOOL_CACHE_TTL_SECONDS in .env)

    # Write tools: a session that used at least procedure_min_write_tools of these (and no
    # errors) gets a procedure proposal. The name predates routing; it no longer picks models.
    sonnet_write_tools: list[str] = [
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
        "delete_kb_source", "ingest_text", "ingest_url",
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
        # Worker code tasks spend Claude Pro limits and (later) open PRs
        "delegate_code_task",
        "draft_email", "send_email", "reply_to_email",
        "delete_event", "delete_task", "delete_file",
        "delete_kb_source", "delete_journal_entry", "delete_subscription",
        "delete_income", "delete_budget",
        # Homelab power actions: confirmed via pending_actions before Proxmox ever sees them
        "restart_container", "start_container",
        "delete_automation",
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
