-- Worker jobs (plans/07): research, second opinions and code tasks run by agents (Claude Code,
-- Antigravity) on the sazed-worker container. Sazed owns the queue and the approval gate; the
-- worker claims queued jobs over HTTP, streams events back, and posts the result.

CREATE TABLE IF NOT EXISTS jobs (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    kind              TEXT NOT NULL,              -- research | second_opinion | code_task
    agent             TEXT NOT NULL,              -- agy | claude
    model             TEXT NOT NULL,
    profile           TEXT NOT NULL,              -- research | review | change
    title             TEXT NOT NULL,              -- short label for lists and notifications
    prompt            TEXT NOT NULL,              -- the full instruction sent to the agent
    -- {"effort": "high", "timeout_s": 300, "max_tool_calls": 40, "repo": ..., "save_to_kb": false}
    params            JSONB NOT NULL DEFAULT '{}'::jsonb,
    status            TEXT NOT NULL,              -- queued | needs_approval | running | succeeded
                                                  -- | failed | cancelled | timed_out
    parent_id         UUID REFERENCES jobs(id) ON DELETE SET NULL,  -- the job a follow-up resumes
    requested_by      TEXT NOT NULL,              -- chat:<session> | automation:<run> | console | mcp
    notify            BOOLEAN NOT NULL DEFAULT FALSE,  -- Pushover on finish (set when the caller stopped waiting)
    agent_session_id  TEXT,                       -- the CLI's own session/conversation id, for resuming
    result            TEXT,
    error             TEXT,
    meta              JSONB NOT NULL DEFAULT '{}'::jsonb,  -- turns, usage, tools used, citations, PR url
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at        TIMESTAMPTZ,
    heartbeat_at      TIMESTAMPTZ,                -- last time the worker reported in for this job
    finished_at       TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_jobs_queue ON jobs (created_at) WHERE status = 'queued';
CREATE INDEX IF NOT EXISTS idx_jobs_recent ON jobs (created_at DESC);

-- The agent's streamed steps (searches, page reads, tool calls), so the console can show what
-- a job did and not just its final answer.
CREATE TABLE IF NOT EXISTS job_events (
    id      BIGSERIAL PRIMARY KEY,
    job_id  UUID NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    event   JSONB NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_job_events_job ON job_events (job_id, id);
