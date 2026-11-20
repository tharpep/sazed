-- The last state the CI watcher read of each PR a card points at, so a board read never calls
-- GitHub. One row per PR; repo is stored lower-case. A merged or closed row is never refreshed.
CREATE TABLE IF NOT EXISTS pr_snapshots (
    repo            TEXT NOT NULL,
    number          INT NOT NULL,
    state           TEXT NOT NULL CHECK (state IN ('open', 'draft', 'merged', 'closed')),
    head_sha        TEXT,
    merged_at       TIMESTAMPTZ,
    additions       INT,
    deletions       INT,
    checks          JSONB,
    review_verdict  TEXT CHECK (review_verdict IN ('approve', 'approve_with_nits', 'request_changes')),
    review_source   TEXT,
    review_url      TEXT,
    fetched_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (repo, number)
);
