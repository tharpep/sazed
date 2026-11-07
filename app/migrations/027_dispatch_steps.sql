-- Dispatch (sazed-worker): the steps of each agent run (tool calls, results, short prose),
-- posted to /dispatch/steps for the console's Plan Live view.
CREATE TABLE IF NOT EXISTS dispatch_steps (
    id           BIGSERIAL PRIMARY KEY,
    plan         TEXT NOT NULL,
    task         TEXT NOT NULL,
    ts           DOUBLE PRECISION NOT NULL,
    seq          INT NOT NULL,
    kind         TEXT NOT NULL,
    tool         TEXT,
    text         TEXT NOT NULL DEFAULT '',
    error        BOOLEAN NOT NULL DEFAULT false,
    received_at  TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (plan, task, ts, seq)
);

CREATE INDEX IF NOT EXISTS dispatch_steps_plan_task_ts ON dispatch_steps (plan, task, ts);
