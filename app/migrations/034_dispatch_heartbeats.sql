-- Dispatch (sazed-worker): the last heartbeat of each running task, posted to
-- /dispatch/heartbeat. The reaper fails a running task once neither a row nor a beat has
-- arrived for DISPATCH_HEARTBEAT_GRACE_S.
CREATE TABLE IF NOT EXISTS dispatch_heartbeats (
    plan     TEXT NOT NULL,
    task     TEXT NOT NULL,
    beat_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (plan, task)
);
