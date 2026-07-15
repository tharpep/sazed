-- Snapshot of the schema that previously lived as a single idempotent SQL
-- blob directly in app/db.py, run in full on every app startup. Every
-- statement here is still IF NOT EXISTS / ADD COLUMN IF NOT EXISTS — safe to
-- apply against a database that already has all of this (which is the case
-- for every existing deployment: this migration's only job on first run
-- there is to get recorded in schema_migrations, not to change anything).
-- Going forward, new schema changes get their own 002_*.sql file instead of
-- growing this one.

CREATE TABLE IF NOT EXISTS sessions (
    id                 UUID PRIMARY KEY,
    created_at         TIMESTAMPTZ DEFAULT NOW(),
    last_activity      TIMESTAMPTZ DEFAULT NOW(),
    message_count      INT DEFAULT 0,
    processed_at       TIMESTAMPTZ,
    summary_kb_id      UUID,
    context_summary    TEXT,
    summarized_through INT DEFAULT 0
);

-- Add columns for existing deployments that predate context windowing
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS context_summary TEXT;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS summarized_through INT DEFAULT 0;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS session_type TEXT DEFAULT 'chat';
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS title TEXT;

-- Serves list_sessions() (WHERE session_type = $1 ORDER BY last_activity DESC,
-- called on every conversation-history open) and the archival job's
-- WHERE last_activity < ... AND session_type = 'chat'. Previously sessions
-- had no index beyond its primary key, so both were full-table scans.
CREATE INDEX IF NOT EXISTS sessions_type_last_activity_idx
    ON sessions (session_type, last_activity DESC);

CREATE TABLE IF NOT EXISTS messages (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id UUID REFERENCES sessions(id) ON DELETE CASCADE,
    role       TEXT NOT NULL,
    content    TEXT NOT NULL,
    timestamp  TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS messages_session_id_timestamp_idx
    ON messages (session_id, timestamp);

CREATE TABLE IF NOT EXISTS archived_sessions (
    id                 UUID PRIMARY KEY,
    created_at         TIMESTAMPTZ,
    last_activity      TIMESTAMPTZ,
    message_count      INT,
    processed_at       TIMESTAMPTZ,
    summary_kb_id      UUID,
    context_summary    TEXT,
    summarized_through INT,
    session_type       TEXT DEFAULT 'chat',
    archived_at        TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE archived_sessions ADD COLUMN IF NOT EXISTS session_type TEXT DEFAULT 'chat';

CREATE TABLE IF NOT EXISTS archived_messages (
    id         UUID PRIMARY KEY,
    session_id UUID NOT NULL,
    role       TEXT NOT NULL,
    content    TEXT NOT NULL,
    timestamp  TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS archived_messages_session_id_idx
    ON archived_messages (session_id);

CREATE TABLE IF NOT EXISTS agent_memory (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    fact_type   TEXT NOT NULL,
    key         TEXT NOT NULL,
    value       TEXT NOT NULL,
    source      TEXT,
    confidence  FLOAT DEFAULT 1.0,
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    updated_at  TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (fact_type, key)
);

-- bi-temporal columns on the live table (idempotent)
ALTER TABLE agent_memory ADD COLUMN IF NOT EXISTS valid_from    TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE agent_memory ADD COLUMN IF NOT EXISTS last_confirmed TIMESTAMPTZ DEFAULT NOW();

-- append-only history of superseded values
CREATE TABLE IF NOT EXISTS agent_memory_history (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    fact_type     TEXT NOT NULL,
    key           TEXT NOT NULL,
    value         TEXT NOT NULL,
    confidence    FLOAT,
    source        TEXT,
    valid_from    TIMESTAMPTZ,
    valid_to      TIMESTAMPTZ DEFAULT NOW(),
    superseded_by TEXT
);
CREATE INDEX IF NOT EXISTS agent_memory_history_key_idx ON agent_memory_history (fact_type, key);

CREATE TABLE IF NOT EXISTS action_logs (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id    UUID REFERENCES sessions(id) ON DELETE CASCADE,
    timestamp     TIMESTAMPTZ DEFAULT NOW(),
    tool_name     TEXT NOT NULL,
    input         JSONB,
    output        TEXT,
    status        TEXT NOT NULL,
    error_message TEXT,
    duration_ms   INT
);

CREATE INDEX IF NOT EXISTS action_logs_session_id_idx ON action_logs (session_id);
CREATE INDEX IF NOT EXISTS action_logs_timestamp_idx  ON action_logs (timestamp DESC);

CREATE TABLE IF NOT EXISTS pending_actions (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id  UUID REFERENCES sessions(id) ON DELETE CASCADE,
    tool_name   TEXT NOT NULL,
    tool_input  JSONB NOT NULL,
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    status      TEXT NOT NULL DEFAULT 'pending'
);
CREATE INDEX IF NOT EXISTS pending_actions_session_idx ON pending_actions (session_id, status);

CREATE TABLE IF NOT EXISTS procedures (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            TEXT NOT NULL,
    trigger_desc    TEXT NOT NULL,
    trigger_keywords TEXT[],
    steps           JSONB NOT NULL,
    status          TEXT NOT NULL DEFAULT 'proposed',
    source_session  UUID,
    use_count       INT DEFAULT 0,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    last_used_at    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS procedures_status_idx ON procedures (status);

CREATE TABLE IF NOT EXISTS llm_calls (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id         UUID REFERENCES sessions(id) ON DELETE CASCADE,
    timestamp          TIMESTAMPTZ DEFAULT NOW(),
    turn               INT,
    model              TEXT NOT NULL,
    purpose            TEXT,
    input_tokens       INT,
    output_tokens      INT,
    cache_read_tokens  INT,
    cache_write_tokens INT,
    duration_ms        INT
);
CREATE INDEX IF NOT EXISTS llm_calls_session_idx ON llm_calls (session_id);
CREATE INDEX IF NOT EXISTS llm_calls_timestamp_idx ON llm_calls (timestamp DESC);
