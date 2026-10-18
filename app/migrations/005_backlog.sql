-- Backlog (plans/07 section 9b): ideas from the scout, plus bugs, gaps, optimizations, planned
-- features and known issues from anyone (chat, console, later the code-health auditor).
-- Nothing is ever deleted: dismissing only sets the status, and the scout dedupes against every
-- item ever recorded so a dismissed idea doesn't come back the next day.

CREATE TABLE IF NOT EXISTS backlog_items (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    type         TEXT NOT NULL,            -- idea | bug | gap | optimization | planned | known_issue
    status       TEXT NOT NULL DEFAULT 'new',
                 -- new | shortlisted | approved | done | dismissed | stale
    title        TEXT NOT NULL,
    summary      TEXT NOT NULL DEFAULT '', -- markdown: what it is
    why          TEXT NOT NULL DEFAULT '', -- why it matters for this setup
    repos        TEXT[] NOT NULL DEFAULT '{}',
    effort       TEXT,                     -- S | M | L
    impact       TEXT,                     -- low | medium | high
    sources      JSONB NOT NULL DEFAULT '[]'::jsonb,  -- [url, ...]
    fingerprint  TEXT NOT NULL,            -- normalized title words, for dedupe
    source       TEXT NOT NULL,            -- scout | chat | console | claude | auditor
    note         TEXT,                     -- Pryce's note (e.g. when approving)
    job_id       UUID REFERENCES jobs(id) ON DELETE SET NULL,  -- the change job it became
    rank         INT,                      -- from the weekly triage (1 = best), null if unranked
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    decided_at   TIMESTAMPTZ               -- when approved or dismissed
);

CREATE INDEX IF NOT EXISTS idx_backlog_open ON backlog_items (type, created_at DESC)
    WHERE status IN ('new', 'shortlisted');
CREATE INDEX IF NOT EXISTS idx_backlog_fingerprint ON backlog_items (fingerprint);
