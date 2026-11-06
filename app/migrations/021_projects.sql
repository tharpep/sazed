-- Projects and workstreams above the board's cards (plans/13, backlog 0a9b633c).
-- A project is an ordered ladder of workstreams; backlog items hang off a workstream.
-- Progress, "next" and "blocked" are computed from the cards, never stored. The only stored
-- states are decisions: a project paused or dropped, a workstream closed as done or dropped.

CREATE TABLE IF NOT EXISTS projects (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    slug        TEXT NOT NULL UNIQUE,
    title       TEXT NOT NULL,
    summary     TEXT NOT NULL DEFAULT '',           -- one plain sentence: what it is
    state       TEXT NOT NULL DEFAULT 'active',     -- active | paused | done | dropped
    rank        INT,                                -- order on the board, null sorts last
    repos       TEXT[] NOT NULL DEFAULT '{}',
    plan        TEXT,                               -- e.g. plans/14
    latest_update    TEXT,                          -- agent-written note on where it stands
    latest_update_at TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS workstreams (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id  UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    title       TEXT NOT NULL,
    summary     TEXT NOT NULL DEFAULT '',
    position    INT NOT NULL,                       -- step on the project's ladder
    state       TEXT NOT NULL DEFAULT 'open',       -- open | done | dropped
    depends_on  UUID[] NOT NULL DEFAULT '{}',       -- workstreams that must be done first
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_workstreams_project ON workstreams (project_id, position);

ALTER TABLE backlog_items
    ADD COLUMN IF NOT EXISTS workstream_id UUID REFERENCES workstreams(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_backlog_workstream ON backlog_items (workstream_id)
    WHERE workstream_id IS NOT NULL;
