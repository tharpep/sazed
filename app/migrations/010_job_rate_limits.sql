-- plans/10 P1.1: the worker now records each Claude job's first and last plan-usage reading in
-- meta.rate_limits (sazed-worker). Backfill the same shape for past jobs from their stored
-- rate_limit_event events. Jobs that already have meta.rate_limits are left alone.

WITH readings AS (
    SELECT e.job_id,
           e.id,
           jsonb_strip_nulls(jsonb_build_object(
               'status', i->>'status',
               'overage', i->'isUsingOverage',
               'overage_status', i->>'overageStatus',
               'five_hour', CASE WHEN i #> '{unifiedWindows,five_hour}' IS NOT NULL THEN
                   jsonb_build_object('utilization', i #> '{unifiedWindows,five_hour,utilization}',
                                      'resets_at', i #> '{unifiedWindows,five_hour,resetsAt}') END,
               'seven_day', CASE WHEN i #> '{unifiedWindows,seven_day}' IS NOT NULL THEN
                   jsonb_build_object('utilization', i #> '{unifiedWindows,seven_day,utilization}',
                                      'resets_at', i #> '{unifiedWindows,seven_day,resetsAt}') END
           )) AS reading
    FROM job_events e, LATERAL (SELECT e.event -> 'rate_limit_info' AS i) r
    WHERE e.event ->> 'type' = 'rate_limit_event'
),
ends AS (
    SELECT DISTINCT job_id,
           first_value(reading) OVER (PARTITION BY job_id ORDER BY id) AS first_reading,
           first_value(reading) OVER (PARTITION BY job_id ORDER BY id DESC) AS last_reading
    FROM readings
)
UPDATE jobs j
SET meta = j.meta || jsonb_build_object(
        'rate_limits', jsonb_build_object('first', ends.first_reading, 'last', ends.last_reading))
FROM ends
WHERE j.id = ends.job_id AND NOT (j.meta ? 'rate_limits');
