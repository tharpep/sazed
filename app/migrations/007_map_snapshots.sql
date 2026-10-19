-- Weekly architecture map snapshots (app/activity/map_report.py): the map's nodes with their
-- health and each line's traffic for the week, so the next run can say what changed.

CREATE TABLE IF NOT EXISTS map_snapshots (
    id    BIGSERIAL PRIMARY KEY,
    at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    data  JSONB NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_map_snapshots_at ON map_snapshots (at DESC);
