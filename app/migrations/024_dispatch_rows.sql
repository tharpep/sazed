-- Dispatch (sazed-worker): each plan and every board row of its parallel agents, posted to
-- /dispatch. run_rows gains a third branch, each dispatch task's latest row. Same columns as 018
-- in the same order; the first two branches are unchanged.
CREATE TABLE IF NOT EXISTS dispatch_plans (
    plan        TEXT PRIMARY KEY,
    repo        TEXT NOT NULL,
    base        TEXT NOT NULL,
    host        TEXT NOT NULL,
    tasks       JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS dispatch_rows (
    id           BIGSERIAL PRIMARY KEY,
    plan         TEXT NOT NULL,
    task         TEXT NOT NULL,
    ts           DOUBLE PRECISION NOT NULL,
    state        TEXT NOT NULL,
    line         TEXT NOT NULL DEFAULT '',
    ref          TEXT,
    cost_usd     DOUBLE PRECISION,
    session      TEXT,
    meta         JSONB NOT NULL DEFAULT '{}'::jsonb,
    received_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (plan, task, ts, state)
);

CREATE INDEX IF NOT EXISTS dispatch_rows_plan_task_ts ON dispatch_rows (plan, task, ts);

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
    l.state                                             AS state,
    CASE WHEN l.state = 'waiting' THEN l.line END       AS question,
    jsonb_strip_nulls(jsonb_build_object('cost_usd', l.cost_usd))          AS usage,
    jsonb_strip_nulls(jsonb_build_object('commit', l.ref))                 AS ref,
    '{}'::jsonb                                         AS checks,
    NULL::text                                          AS variant,
    NULL::text                                          AS failure_class,
    NULL::text                                          AS job_id,
    to_timestamp(l.first_ts)                            AS created_at,
    to_timestamp(l.running_ts)                          AS started_at,
    to_timestamp(l.settled_ts)                          AS finished_at,
    NULL::boolean                                       AS check_passed
FROM (
    SELECT DISTINCT ON (r.plan, r.task)
        r.plan, r.task, r.state, r.line, r.ref, r.cost_usd,
        MIN(r.ts) OVER w AS first_ts,
        MAX(r.ts) FILTER (WHERE r.state = 'running') OVER w AS running_ts,
        MAX(r.ts) FILTER (
            WHERE r.state IN ('done', 'failed', 'landed', 'conflict', 'blocked', 'cleaned')
        ) OVER w AS settled_ts
    FROM dispatch_rows r
    WINDOW w AS (PARTITION BY r.plan, r.task)
    ORDER BY r.plan, r.task, r.ts DESC, r.id DESC
) l
LEFT JOIN LATERAL (
    SELECT t AS def
    FROM dispatch_plans p, jsonb_array_elements(p.tasks) AS t
    WHERE p.plan = l.plan AND t->>'id' = l.task
    LIMIT 1
) d ON TRUE;
