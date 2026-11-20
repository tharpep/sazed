-- The last state the CI watcher read of each PR a card points at, so a board read never calls
-- GitHub. One row per PR; repo is stored lower-case. A merged or closed row is never refreshed.
-- A failed fetch counts in failures and stamps attempted_at, so the ref backs off; state and
-- fetched_at stay NULL until the first fetch that works, and keep the last good read after.
CREATE TABLE IF NOT EXISTS pr_snapshots (
    repo            TEXT NOT NULL,
    number          INT NOT NULL,
    state           TEXT CHECK (state IN ('open', 'draft', 'merged', 'closed')),
    head_sha        TEXT,
    merged_at       TIMESTAMPTZ,
    additions       INT,
    deletions       INT,
    checks          JSONB,
    review_verdict  TEXT CHECK (review_verdict IN ('approve', 'approve_with_nits', 'request_changes')),
    review_source   TEXT,
    review_url      TEXT,
    fetched_at      TIMESTAMPTZ,
    attempted_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    failures        INT NOT NULL DEFAULT 0,
    PRIMARY KEY (repo, number)
);
