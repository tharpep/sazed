-- Plan window readings from more than worker jobs. The homelab probe and dispatch post a
-- rate_limit_info object, laptops post activity so the probe reads only while Claude is in use,
-- and plan periods price the subscription against API-equivalent usage.
CREATE OR REPLACE FUNCTION plan_limit_readings_insert(
    info JSONB, at TIMESTAMPTZ, src TEXT, host TEXT, job UUID, event_id BIGINT
) RETURNS INT AS $$
    WITH added AS (
        INSERT INTO plan_limit_readings
            (read_at, plan_window, used_pct, resets_at, status, source, machine, job_id, job_event_id)
        SELECT at, w.key, (w.value->>'utilization')::real * 100,
               to_timestamp((w.value->>'resetsAt')::double precision),
               info->>'status', src, host, job, event_id
        FROM jsonb_each(CASE WHEN jsonb_typeof(info->'unifiedWindows') = 'object'
                             THEN info->'unifiedWindows' END) AS w
        WHERE w.key IN ('five_hour', 'seven_day')
          AND jsonb_typeof(w.value->'utilization') = 'number'
        ON CONFLICT DO NOTHING
        RETURNING 1
    )
    SELECT count(*)::int FROM added;
$$ LANGUAGE sql;

CREATE OR REPLACE FUNCTION plan_limit_readings_from_event() RETURNS trigger AS $$
BEGIN
    PERFORM plan_limit_readings_insert(
        NEW.event->'rate_limit_info', NEW.at, 'job', 'worker', NEW.job_id, NEW.id);
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE UNIQUE INDEX IF NOT EXISTS plan_limit_readings_posted_idx
    ON plan_limit_readings (source, machine, plan_window, read_at) WHERE job_event_id IS NULL;

CREATE TABLE IF NOT EXISTS claude_activity (
    machine  TEXT PRIMARY KEY,
    last_at  TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS claude_plan_periods (
    starts_on    DATE PRIMARY KEY,
    plan         TEXT NOT NULL,
    monthly_usd  DOUBLE PRECISION NOT NULL CHECK (monthly_usd >= 0),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
