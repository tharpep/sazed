-- run_rows reads the worker's one check summary (My-AI plans/14, the shared check result):
-- meta.check_result on change jobs from sazed-worker's check_result.py onward. Older jobs keep the
-- scattered fields 017 read, and check_passed stays NULL for them. Same columns as 017 in the same
-- order (CREATE OR REPLACE VIEW can only add columns at the end), plus check_passed.
CREATE OR REPLACE VIEW run_rows AS
SELECT
    j.id::text                                          AS id,
    'job'::text                                         AS shape,
    j.parent_id::text                                   AS parent,
    j.requested_by                                      AS source,
    j.agent,
    j.model,
    j.profile,
    j.title,
    j.status                                            AS state,
    j.meta->>'question'                                 AS question,
    jsonb_strip_nulls(jsonb_build_object(
        'tokens', j.meta->'usage', 'plan_limits', j.meta->'rate_limits'))  AS usage,
    jsonb_strip_nulls(jsonb_build_object(
        'branch', j.meta->'branch', 'head_sha', j.meta->'head_sha',
        'pr_number', j.meta->'pr_number', 'pr_url', j.meta->'pr_url'))     AS ref,
    COALESCE(j.meta->'check_result', jsonb_strip_nulls(jsonb_build_object(
        'checks', j.meta->'checks', 'tests', j.meta->'tests')))           AS checks,
    j.variant,
    j.failure_class,
    j.id::text                                          AS job_id,
    j.created_at,
    j.started_at,
    j.finished_at,
    (j.meta->'check_result'->>'passed')::boolean        AS check_passed
FROM jobs j
UNION ALL
SELECT
    r.id::text || ':' || (s.n - 1)                      AS id,
    'step'::text                                        AS shape,
    r.id::text                                          AS parent,
    'automation-run:' || r.id                           AS source,
    sj.agent,
    sj.model,
    sj.profile,
    s.step->>'name'                                     AS title,
    CASE s.step->>'status'
        WHEN 'ok' THEN 'succeeded'
        WHEN 'approved' THEN 'succeeded'
        WHEN 'error' THEN 'failed'
        WHEN 'stopped' THEN 'skipped'
        WHEN 'paused' THEN 'needs_approval'
        ELSE s.step->>'status'
    END                                                 AS state,
    a.title                                             AS question,
    jsonb_strip_nulls(jsonb_build_object('tokens', s.step->'tokens'))      AS usage,
    '{}'::jsonb                                         AS ref,
    '{}'::jsonb                                         AS checks,
    NULL::text                                          AS variant,
    NULL::text                                          AS failure_class,
    s.step->>'job'                                      AS job_id,
    r.started_at                                        AS created_at,
    NULL::timestamptz                                   AS started_at,
    NULL::timestamptz                                   AS finished_at,
    NULL::boolean                                       AS check_passed
FROM automation_runs r
CROSS JOIN LATERAL jsonb_array_elements(r.steps) WITH ORDINALITY AS s(step, n)
LEFT JOIN jobs sj ON sj.id::text = s.step->>'job'
LEFT JOIN approvals a ON a.id::text = s.step->>'approval' AND s.step->>'status' = 'paused';
