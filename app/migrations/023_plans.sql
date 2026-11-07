-- Plans on the board (My-AI plans/18): a plan belongs to a project, every save is a full new
-- version with an author and a reason, and cards point at the plan section they came from.

CREATE TABLE IF NOT EXISTS plans (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    slug            TEXT NOT NULL UNIQUE,
    title           TEXT NOT NULL,
    project_id      UUID REFERENCES projects(id) ON DELETE SET NULL,
    workstream_id   UUID REFERENCES workstreams(id) ON DELETE SET NULL,
    state           TEXT NOT NULL DEFAULT 'draft',  -- draft | active | done | superseded
    current_version INT NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS plan_versions (
    plan_id     UUID NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
    version     INT NOT NULL,
    body        TEXT NOT NULL,
    author      TEXT NOT NULL,   -- pryce | agent:<name> | dispatch:<run>
    reason      TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (plan_id, version)
);

CREATE INDEX IF NOT EXISTS idx_plans_project ON plans (project_id);

ALTER TABLE backlog_items
    ADD COLUMN IF NOT EXISTS plan_id UUID REFERENCES plans(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS plan_section TEXT,
    ADD COLUMN IF NOT EXISTS plan_version INT;

CREATE INDEX IF NOT EXISTS idx_backlog_plan ON backlog_items (plan_id) WHERE plan_id IS NOT NULL;
