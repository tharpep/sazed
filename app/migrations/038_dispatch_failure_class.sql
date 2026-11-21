-- One stack, seam 5 (decision 10): the one place a dispatch row becomes a failure class of
-- app/learning/classes.py, from the reason code the worker writes in meta.reason (the closed list
-- in sazed-worker's dispatch/reasons.py) or the reaper writes in meta.stop_reason, never from the
-- line text (pinned by tests/test_dispatch_failure_class.py). Only a red row has a class. A
-- dependency or overlap code means the task itself did not fail, so it has none. A red row with
-- no code or an unknown one is unclassified, which never proposes a guard. CREATE OR REPLACE
-- keeps a second run a no-op.
CREATE OR REPLACE FUNCTION dispatch_failure_class(raw text, meta jsonb)
RETURNS text LANGUAGE sql IMMUTABLE AS $$
SELECT CASE WHEN raw IN ('failed', 'blocked', 'conflict', 'timed_out') THEN
    CASE
        WHEN 'no_heartbeat' IN (meta->>'reason', meta->>'stop_reason') THEN 'worker_lost'
        ELSE CASE meta->>'reason'
            WHEN 'timeout' THEN 'timed_out'
            WHEN 'plan_limit' THEN 'rate_limit'
            WHEN 'tool_cap' THEN 'tool_cap'
            WHEN 'cancelled' THEN 'cancelled'
            WHEN 'budget' THEN 'budget'
            WHEN 'no_change' THEN 'no_change'
            WHEN 'check_failed' THEN 'checks_failed'
            WHEN 'no_result' THEN 'agent_error'
            WHEN 'no_done_state' THEN 'result_unusable'
            WHEN 'max_tokens' THEN 'result_unusable'
            WHEN 'wrapper_failed' THEN 'worker_error'
            WHEN 'regen_failed' THEN 'worker_error'
            WHEN 'left_owns' THEN 'left_owns'
            WHEN 'conflict' THEN 'land_conflict'
            WHEN 'no_evidence' THEN 'no_evidence'
            WHEN 'refused' THEN 'refused'
            WHEN 'dependency_pending' THEN NULL
            WHEN 'dependency_failed' THEN NULL
            WHEN 'overlap' THEN NULL
            ELSE 'unclassified'
        END
    END
END
$$;

-- run_rows as in 035, except the task branch's failure_class: the class of the task's latest row
-- that is not cleaned, so cleaning a failed task keeps its class.
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
    NULL::text                                          AS job_id,
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
) d ON TRUE;
