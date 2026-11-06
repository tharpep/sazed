-- A result-only change job's files, kept instead of opening a PR (My-AI plans/14, bake-offs). Each
-- bake-off arm stores its changes here; promoting the winning arm opens one PR from them. The
-- content is the worker's finish payload as sent: path -> base64 content, or null for a deletion.
CREATE TABLE IF NOT EXISTS job_changes (
    job_id    UUID PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
    changes   JSONB NOT NULL,
    saved_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
