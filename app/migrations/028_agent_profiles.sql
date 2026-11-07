-- Agent profiles (dispatch): model, effort, budget, timeout and write access per kind of task,
-- with a history row for every save. Dispatch plans also link to a board project and plan.
CREATE TABLE IF NOT EXISTS agent_profiles (
    name              TEXT PRIMARY KEY,
    model             TEXT NOT NULL,
    effort            TEXT NOT NULL,
    budget_usd        DOUBLE PRECISION NOT NULL,
    timeout_s         INT NOT NULL,
    read_only         BOOLEAN NOT NULL,
    evidence_required BOOLEAN NOT NULL DEFAULT false,
    description       TEXT NOT NULL DEFAULT '',
    version           INT NOT NULL,
    updated_by        TEXT NOT NULL,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS agent_profile_versions (
    id       BIGSERIAL PRIMARY KEY,
    name     TEXT NOT NULL,
    version  INT NOT NULL,
    author   TEXT NOT NULL,
    reason   TEXT NOT NULL,
    profile  JSONB NOT NULL,
    at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (name, version)
);

ALTER TABLE dispatch_plans
    ADD COLUMN IF NOT EXISTS project TEXT,
    ADD COLUMN IF NOT EXISTS board_plan TEXT;

CREATE INDEX IF NOT EXISTS idx_dispatch_plans_project ON dispatch_plans (project)
    WHERE project IS NOT NULL;

INSERT INTO agent_profiles
    (name, model, effort, budget_usd, timeout_s, read_only, evidence_required, description,
     version, updated_by)
VALUES
    ('smoke', 'haiku', 'medium', 0.5, 600, true, false,
     'Checks a sweep''s output shape before paying for max.', 1, 'pryce'),
    ('sweep', 'haiku', 'max', 0.5, 600, true, false,
     'Literal extraction over one small chunk.', 1, 'pryce'),
    ('verify', 'haiku', 'max', 0.5, 600, true, true,
     'Confirms or refutes one finding set; must read evidence.', 1, 'pryce'),
    ('implement', 'sonnet', 'high', 2.0, 1200, false, false,
     'Code behind a check.', 1, 'pryce'),
    ('design', 'opus', 'xhigh', 4.0, 1200, false, false,
     'Interfaces, reconcile, the verdict when two verify runs disagree.', 1, 'pryce')
ON CONFLICT (name) DO NOTHING;

INSERT INTO agent_profile_versions (name, version, author, reason, profile)
SELECT name, 1, 'pryce', 'seeded from the dispatch bake-off table',
       jsonb_build_object(
           'model', model, 'effort', effort, 'budget_usd', budget_usd, 'timeout_s', timeout_s,
           'read_only', read_only, 'evidence_required', evidence_required,
           'description', description)
FROM agent_profiles
WHERE name IN ('smoke', 'sweep', 'verify', 'implement', 'design') AND version = 1
ON CONFLICT (name, version) DO NOTHING;
