-- Captures: one inbox for quick thoughts, rambles, notes, and tasks.
-- Stores raw text verbatim first, then extracts structured items (journal,
-- tasks, backlog, KB) asynchronously.

CREATE TABLE IF NOT EXISTS captures (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    content      TEXT NOT NULL,
    source       TEXT NOT NULL DEFAULT 'shortcut',
    status       TEXT NOT NULL DEFAULT 'raw',
    routed_to    JSONB NOT NULL DEFAULT '[]'::jsonb,
    error        TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    processed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_captures_created ON captures (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_captures_status ON captures (status);
