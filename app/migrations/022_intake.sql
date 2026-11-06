-- The router's intake (plans/16): every inbound event is recorded with what code decided about it,
-- and a thread (a GitHub issue or PR) remembers the work it started, so a later comment on it
-- continues that work instead of starting new work.

CREATE TABLE IF NOT EXISTS intake_triggers (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source       TEXT NOT NULL,                -- github
    event        TEXT NOT NULL,                -- pull_request | issues | issue_comment | ...
    action       TEXT NOT NULL DEFAULT '',
    actor        TEXT NOT NULL DEFAULT '',
    thread_key   TEXT NOT NULL,                -- github:<owner/repo>#<number>
    decision     TEXT NOT NULL,                -- accept | observe | ignore
    reason       TEXT NOT NULL,
    action_taken TEXT,                         -- review | follow_up | new_task | backlog_card
    job_id       UUID REFERENCES jobs(id) ON DELETE SET NULL,
    item_id      UUID REFERENCES backlog_items(id) ON DELETE SET NULL,
    received_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_intake_triggers_recent ON intake_triggers (received_at DESC);
CREATE INDEX IF NOT EXISTS idx_intake_triggers_thread ON intake_triggers (thread_key, received_at DESC);

CREATE TABLE IF NOT EXISTS intake_threads (
    thread_key  TEXT PRIMARY KEY,
    job_id      UUID REFERENCES jobs(id) ON DELETE SET NULL,          -- first job the thread started
    item_id     UUID REFERENCES backlog_items(id) ON DELETE SET NULL, -- or the card it filed
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
