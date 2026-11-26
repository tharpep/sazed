-- D7 stage 1: a dispatch plan can run as one worker job (kind 'dispatch'), submitted by
-- POST /dispatch/plans/{plan}/submit and approved like any other job. job_id is the plan's newest
-- dispatch job; a plan only ever run by hand keeps NULL. The worker-key mirror and the export gate
-- read it, so only submit writes it. Every statement is guarded, so a second run changes nothing.
ALTER TABLE dispatch_plans
    ADD COLUMN IF NOT EXISTS job_id UUID REFERENCES jobs(id) ON DELETE SET NULL;

CREATE OR REPLACE FUNCTION activity_job_label(p_kind TEXT) RETURNS TEXT AS $$
    SELECT CASE p_kind
        WHEN 'research' THEN 'Research'
        WHEN 'second_opinion' THEN 'Second opinion'
        WHEN 'code_task' THEN 'Code task'
        WHEN 'dispatch' THEN 'Dispatch'
        ELSE initcap(replace(p_kind, '_', ' ')) END
$$ LANGUAGE sql IMMUTABLE;

-- run_rows as in 038, except the task branch's job_id: the plan's dispatch job.
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
LEFT JOIN approvals a ON a.id::text = s.step->>'approval' AND s.step->>'status' = 'paused'
UNION ALL
SELECT
    l.plan || ':' || l.task                             AS id,
    'task'::text                                        AS shape,
    l.plan                                              AS parent,
    'dispatch:' || l.plan                               AS source,
    NULL::text                                          AS agent,
    d.def->>'model'                                     AS model,
    d.def->>'profile'                                   AS profile,
    l.task                                              AS title,
    dispatch_state(l.state, l.meta, b.state, b.meta)    AS state,
    CASE WHEN l.state = 'waiting' THEN l.line END       AS question,
    jsonb_strip_nulls(jsonb_build_object('cost_usd', l.cost_usd))          AS usage,
    jsonb_strip_nulls(jsonb_build_object('commit', l.ref))                 AS ref,
    '{}'::jsonb                                         AS checks,
    NULL::text                                          AS variant,
    CASE WHEN l.state = 'cleaned' THEN dispatch_failure_class(b.state, b.meta)
        ELSE dispatch_failure_class(l.state, l.meta) END AS failure_class,
    dp.job_id::text                                     AS job_id,
    to_timestamp(l.first_ts)                            AS created_at,
    to_timestamp(l.running_ts)                          AS started_at,
    to_timestamp(l.settled_ts)                          AS finished_at,
    NULL::boolean                                       AS check_passed
FROM (
    SELECT DISTINCT ON (r.plan, r.task)
        r.plan, r.task, r.state, r.line, r.ref, r.cost_usd, r.meta,
        MIN(r.ts) OVER w AS first_ts,
        MAX(r.ts) FILTER (WHERE r.state = 'running') OVER w AS running_ts,
        MAX(r.ts) FILTER (
            WHERE r.state IN (
                'done', 'failed', 'landed', 'conflict', 'blocked', 'cleaned', 'timed_out'
            )
        ) OVER w AS settled_ts
    FROM dispatch_rows r
    WINDOW w AS (PARTITION BY r.plan, r.task)
    ORDER BY r.plan, r.task, r.ts DESC, r.id DESC
) l
LEFT JOIN LATERAL (
    SELECT p.state, p.meta
    FROM dispatch_rows p
    WHERE l.state = 'cleaned' AND p.plan = l.plan AND p.task = l.task AND p.state <> 'cleaned'
    ORDER BY p.ts DESC, p.id DESC
    LIMIT 1
) b ON TRUE
LEFT JOIN LATERAL (
    SELECT t AS def
    FROM dispatch_plans p, jsonb_array_elements(p.tasks) AS t
    WHERE p.plan = l.plan AND t->>'id' = l.task
    LIMIT 1
) d ON TRUE
LEFT JOIN dispatch_plans dp ON dp.plan = l.plan;
