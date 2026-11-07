-- Plan window readings (5-hour and 7-day utilisation of Pryce's Claude plan) for the usage hub.
-- Worker jobs stream rate_limit_event events into job_events; a trigger copies each one here, so
-- the hub reads a small table instead of scanning every job event. Laptop readings (statusline)
-- can land here later with job_event_id NULL.
CREATE TABLE IF NOT EXISTS plan_limit_readings (
    id            BIGSERIAL PRIMARY KEY,
    read_at       TIMESTAMPTZ NOT NULL,
    plan_window   TEXT NOT NULL CHECK (plan_window IN ('five_hour', 'seven_day')),
    used_pct      REAL NOT NULL,
    resets_at     TIMESTAMPTZ,
    status        TEXT,
    source        TEXT NOT NULL,
    machine       TEXT,
    job_id        UUID REFERENCES jobs(id) ON DELETE SET NULL,
    job_event_id  BIGINT REFERENCES job_events(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS plan_limit_readings_window_idx
    ON plan_limit_readings (plan_window, read_at DESC);

-- One event carries both windows, so the key is (event, window).
CREATE UNIQUE INDEX IF NOT EXISTS plan_limit_readings_event_window_idx
    ON plan_limit_readings (job_event_id, plan_window) WHERE job_event_id IS NOT NULL;

CREATE OR REPLACE FUNCTION plan_limit_readings_from_event() RETURNS trigger AS $$
BEGIN
    INSERT INTO plan_limit_readings
        (read_at, plan_window, used_pct, resets_at, status, source, machine, job_id, job_event_id)
    SELECT NEW.at, w.key, (w.value->>'utilization')::real * 100,
           to_timestamp((w.value->>'resetsAt')::double precision),
           NEW.event->'rate_limit_info'->>'status', 'job', 'worker', NEW.job_id, NEW.id
    FROM jsonb_each(NEW.event->'rate_limit_info'->'unifiedWindows') AS w
    WHERE w.key IN ('five_hour', 'seven_day')
      AND jsonb_typeof(w.value->'utilization') = 'number'
    ON CONFLICT DO NOTHING;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS plan_limit_readings_trg ON job_events;
CREATE TRIGGER plan_limit_readings_trg AFTER INSERT ON job_events
    FOR EACH ROW WHEN (NEW.event->>'type' = 'rate_limit_event')
    EXECUTE FUNCTION plan_limit_readings_from_event();

-- Backfill from the readings already stored.
INSERT INTO plan_limit_readings
    (read_at, plan_window, used_pct, resets_at, status, source, machine, job_id, job_event_id)
SELECT e.at, w.key, (w.value->>'utilization')::real * 100,
       to_timestamp((w.value->>'resetsAt')::double precision),
       e.event->'rate_limit_info'->>'status', 'job', 'worker', e.job_id, e.id
FROM job_events e, jsonb_each(e.event->'rate_limit_info'->'unifiedWindows') AS w
WHERE e.event->>'type' = 'rate_limit_event'
  AND w.key IN ('five_hour', 'seven_day')
  AND jsonb_typeof(w.value->'utilization') = 'number'
ON CONFLICT DO NOTHING;
