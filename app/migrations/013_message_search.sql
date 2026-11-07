-- Cross-session recall: full-text search over past messages (search_past_sessions tool).
-- A stored generated column keeps the tsvector in step with every insert, so no code path
-- that writes messages needs to change. The column is rebuilt for existing rows on apply.

ALTER TABLE messages
    ADD COLUMN IF NOT EXISTS content_tsv tsvector
    GENERATED ALWAYS AS (to_tsvector('english', content)) STORED;

CREATE INDEX IF NOT EXISTS messages_content_tsv_idx ON messages USING GIN (content_tsv);
