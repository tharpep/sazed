CREATE TABLE IF NOT EXISTS claude_usage_sessions (
    machine     TEXT NOT NULL,
    session_id  TEXT NOT NULL,
    project     TEXT,
    title       TEXT,
    agent_name  TEXT,
    version     TEXT,
    first_ts    TIMESTAMPTZ NOT NULL,
    last_ts     TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (machine, session_id)
);
CREATE INDEX IF NOT EXISTS claude_usage_sessions_first_idx
    ON claude_usage_sessions (first_ts DESC);

CREATE TABLE IF NOT EXISTS claude_usage_prompts (
    machine          TEXT NOT NULL,
    uuid             TEXT NOT NULL,
    session_id       TEXT NOT NULL,
    ts               TIMESTAMPTZ NOT NULL,
    chars            INTEGER NOT NULL,
    source           TEXT,
    permission_mode  TEXT,
    confused         BOOLEAN NOT NULL,
    PRIMARY KEY (machine, uuid)
);
CREATE INDEX IF NOT EXISTS claude_usage_prompts_ts_idx ON claude_usage_prompts (ts);
CREATE INDEX IF NOT EXISTS claude_usage_prompts_session_idx
    ON claude_usage_prompts (machine, session_id, ts);

CREATE TABLE IF NOT EXISTS claude_usage_responses (
    machine             TEXT NOT NULL,
    uuid                TEXT NOT NULL,
    session_id          TEXT NOT NULL,
    ts                  TIMESTAMPTZ NOT NULL,
    model               TEXT,
    effort              TEXT,
    output_style        TEXT,
    input_tokens        BIGINT NOT NULL,
    output_tokens       BIGINT NOT NULL,
    cache_read_tokens   BIGINT NOT NULL,
    cache_write_tokens  BIGINT NOT NULL,
    sidechain           BOOLEAN NOT NULL,
    context             BIGINT GENERATED ALWAYS AS
        (input_tokens + cache_read_tokens + cache_write_tokens) STORED,
    PRIMARY KEY (machine, uuid)
);
CREATE INDEX IF NOT EXISTS claude_usage_responses_ts_idx ON claude_usage_responses (ts);
CREATE INDEX IF NOT EXISTS claude_usage_responses_main_idx
    ON claude_usage_responses (machine, session_id, ts) WHERE NOT sidechain;

CREATE TABLE IF NOT EXISTS claude_usage_tool_calls (
    machine      TEXT NOT NULL,
    tool_use_id  TEXT NOT NULL,
    session_id   TEXT NOT NULL,
    ts           TIMESTAMPTZ NOT NULL,
    name         TEXT NOT NULL,
    detail       TEXT,
    writes_files BOOLEAN NOT NULL,
    sidechain    BOOLEAN NOT NULL,
    PRIMARY KEY (machine, tool_use_id)
);
CREATE INDEX IF NOT EXISTS claude_usage_tool_calls_ts_idx ON claude_usage_tool_calls (ts, name);

CREATE TABLE IF NOT EXISTS claude_usage_events (
    machine     TEXT NOT NULL,
    uuid        TEXT NOT NULL,
    session_id  TEXT NOT NULL,
    ts          TIMESTAMPTZ NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('compact', 'interrupt', 'rejection')),
    PRIMARY KEY (machine, uuid)
);
CREATE INDEX IF NOT EXISTS claude_usage_events_ts_idx ON claude_usage_events (ts, kind);

CREATE TABLE IF NOT EXISTS claude_usage_slash_commands (
    machine  TEXT NOT NULL,
    ts       TIMESTAMPTZ NOT NULL,
    project  TEXT NOT NULL,
    command  TEXT NOT NULL,
    PRIMARY KEY (machine, ts, project, command)
);
CREATE INDEX IF NOT EXISTS claude_usage_slash_commands_ts_idx
    ON claude_usage_slash_commands (ts);
