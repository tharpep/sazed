-- Claude Code sessions reporting through hooks (slice 3, POST /sessions/events). One row per
-- session holds what the console reads; the newest-wins columns carry the (ts, seq) of the event
-- that set them, so a late batch never overwrites a newer state. last_heard_at is Sazed's clock,
-- so "not heard from" never depends on a machine's clock. Rows are never deleted.
CREATE TABLE IF NOT EXISTS agent_sessions (
    session_id     TEXT PRIMARY KEY,
    host           TEXT,
    cwd            TEXT,
    repo           TEXT,
    branch         TEXT,
    card_id        UUID,
    started_at     TIMESTAMPTZ NOT NULL,
    last_heard_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    state          TEXT NOT NULL,
    state_ts       DOUBLE PRECISION NOT NULL,
    state_seq      INT NOT NULL,
    ended_at       TIMESTAMPTZ,
    doing          TEXT,
    doing_source   TEXT,
    doing_ts       DOUBLE PRECISION,
    doing_seq      INT,
    question       TEXT,
    question_ts    DOUBLE PRECISION,
    question_seq   INT,
    cleared_ts     DOUBLE PRECISION,
    cleared_seq    INT
);

CREATE INDEX IF NOT EXISTS agent_sessions_last_heard ON agent_sessions (last_heard_at DESC);

-- Each agent's steps in a session; agent_id '' is the main thread. Pruned on insert to the
-- newest 80 per (session, agent), so a chatty session cannot grow the table without bound.
CREATE TABLE IF NOT EXISTS agent_session_events (
    id           BIGSERIAL PRIMARY KEY,
    session_id   TEXT NOT NULL REFERENCES agent_sessions (session_id),
    agent_id     TEXT NOT NULL DEFAULT '',
    agent_type   TEXT,
    ts           DOUBLE PRECISION NOT NULL,
    seq          INT NOT NULL,
    kind         TEXT NOT NULL,
    tool         TEXT,
    target       TEXT,
    text         TEXT NOT NULL DEFAULT '',
    error        BOOLEAN NOT NULL DEFAULT false,
    received_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (session_id, ts, seq)
);

CREATE INDEX IF NOT EXISTS agent_session_events_agent
    ON agent_session_events (session_id, agent_id, ts DESC, seq DESC);
