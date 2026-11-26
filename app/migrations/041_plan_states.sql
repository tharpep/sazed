-- Plan states (slice 4): a card's plan approved, then started, each with who and when. Approval
-- records the plan, section and current_version it approved, so moving the card to another plan
-- or section, or a later save that changes its section, makes it stale. Every column is nullable
-- and every statement idempotent, and nothing here depends on 040, so this applies on a database
-- with or without it.
ALTER TABLE backlog_items
    ADD COLUMN IF NOT EXISTS plan_approved_at      TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS plan_approved_by      TEXT,
    ADD COLUMN IF NOT EXISTS plan_approved_plan_id UUID,
    ADD COLUMN IF NOT EXISTS plan_approved_section TEXT,
    ADD COLUMN IF NOT EXISTS plan_approved_version INT,
    ADD COLUMN IF NOT EXISTS started_at            TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS started_by            TEXT;

-- The card's history: one row per approval, start and withdrawal, never updated or deleted.
CREATE TABLE IF NOT EXISTS card_events (
    id      BIGSERIAL PRIMARY KEY,
    item_id UUID REFERENCES backlog_items (id),
    at      TIMESTAMPTZ DEFAULT NOW(),
    kind    TEXT,
    actor   TEXT,
    detail  JSONB
);

CREATE INDEX IF NOT EXISTS card_events_item ON card_events (item_id, at);

-- What a reporting session says it is; only 'orchestrator' is stored today.
ALTER TABLE agent_sessions ADD COLUMN IF NOT EXISTS role TEXT;
