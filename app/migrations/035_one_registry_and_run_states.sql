-- One stack, seam 1 (decisions 6 and 7): one profile registry for jobs and dispatch, and one
-- function that maps a dispatch row onto the run-state vocabulary of migration 017.
-- Each step is guarded so a second run changes nothing; the runner's advisory lock already
-- keeps two hosts from applying it at once.

ALTER TABLE agent_profiles
    ADD COLUMN IF NOT EXISTS engine TEXT NOT NULL DEFAULT 'claude',
    ADD COLUMN IF NOT EXISTS capability TEXT,
    ADD COLUMN IF NOT EXISTS max_tool_calls INT;

-- read_only becomes a view of capability, so workers and consoles that read it keep working.
-- Existing rows keep their meaning: writable rows change code, read-only rows review it.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = current_schema() AND table_name = 'agent_profiles'
          AND column_name = 'read_only' AND is_generated = 'NEVER'
    ) THEN
        UPDATE agent_profiles
        SET capability = CASE WHEN read_only THEN 'review' ELSE 'change' END
        WHERE capability IS NULL;
        ALTER TABLE agent_profiles DROP COLUMN read_only;
    END IF;
END $$;

ALTER TABLE agent_profiles
    ALTER COLUMN capability SET NOT NULL,
    ALTER COLUMN effort DROP NOT NULL,
    ALTER COLUMN budget_usd DROP NOT NULL,
    ADD COLUMN IF NOT EXISTS read_only BOOLEAN GENERATED ALWAYS AS (capability <> 'change') STORED,
    DROP CONSTRAINT IF EXISTS agent_profiles_engine_check,
    ADD CONSTRAINT agent_profiles_engine_check CHECK (engine IN ('claude', 'agy')),
    DROP CONSTRAINT IF EXISTS agent_profiles_capability_check,
    ADD CONSTRAINT agent_profiles_capability_check
        CHECK (capability IN ('research', 'review', 'change', 'browse'));

-- The job profiles carry the values of the code table they replace (pinned by
-- tests/test_job_profile_seeds.py). Jobs pass no budget or effort yet, hence NULL. flash is the
-- worker's built-in profile, complete because the old worker swaps a built-in for the registry
-- row of the same name whole.
INSERT INTO agent_profiles
    (name, engine, capability, model, effort, budget_usd, timeout_s, max_tool_calls,
     evidence_required, description, version, updated_by)
VALUES
    ('research_quick', 'agy', 'research', 'gemini-3.8-flash-high', NULL, NULL, 300, 30, false,
     'Quick research job: a direct answer from a web search.', 1, 'pryce'),
    ('research_deep', 'agy', 'research', 'gemini-3.8-flash-high', NULL, NULL, 600, 80, false,
     'Deep research job: a sourced report.', 1, 'pryce'),
    ('second_opinion', 'agy', 'research', 'gemini-3.8-flash-high', NULL, NULL, 300, 20, false,
     'Second-opinion job: an independent critique.', 1, 'pryce'),
    ('review', 'claude', 'review', 'claude-sonnet-5-5', NULL, NULL, 900, 150, false,
     'Code review job: reads a repo or PR, edits nothing.', 1, 'pryce'),
    ('change', 'claude', 'change', 'claude-sonnet-5-5', NULL, NULL, 1800, 200, false,
     'Code change job: edits a repo and opens a PR.', 1, 'pryce'),
    ('flash', 'agy', 'change', 'gemini-3.8-flash-high', 'high', 0.5, 1200, NULL, false,
     'Dispatch task on Gemini Flash.', 1, 'pryce')
ON CONFLICT (name) DO NOTHING;

INSERT INTO agent_profile_versions (name, version, author, reason, profile)
SELECT name, 1, 'pryce', 'seeded from the job profile table and the worker''s flash profile',
       jsonb_build_object(
           'engine', engine, 'capability', capability, 'model', model, 'effort', effort,
           'budget_usd', budget_usd, 'timeout_s', timeout_s, 'max_tool_calls', max_tool_calls,
           'evidence_required', evidence_required, 'description', description, 'mcp', mcp)
FROM agent_profiles
WHERE name IN ('research_quick', 'research_deep', 'second_opinion', 'review', 'change', 'flash')
  AND version = 1
ON CONFLICT (name, version) DO NOTHING;

-- The one place a dispatch row's raw word becomes a run state (pinned by
-- tests/test_dispatch_state.py). The raw word survives as the phase. A cleaned row is
-- housekeeping: it takes the state of the latest row before it that is not cleaned (before,
-- before_meta), except that cleaning a running or waiting task, or one that never ran, cancels
-- it. The reaper writes its reason as meta.stop_reason, the worker as meta.reason.
CREATE OR REPLACE FUNCTION dispatch_state(
    raw text, meta jsonb, before text DEFAULT NULL, before_meta jsonb DEFAULT NULL
) RETURNS text LANGUAGE plpgsql IMMUTABLE AS $$
BEGIN
    IF raw = 'cleaned' THEN
        IF before IS NULL OR before IN ('running', 'waiting') THEN
            RETURN 'cancelled';
        END IF;
        raw := before;
        meta := before_meta;
    END IF;
    RETURN CASE raw
        WHEN 'running' THEN 'running'
        WHEN 'waiting' THEN 'needs_input'
        WHEN 'done' THEN 'succeeded'
        WHEN 'landed' THEN 'succeeded'
        WHEN 'conflict' THEN 'failed'
        WHEN 'failed' THEN
            CASE WHEN 'no_heartbeat' IN (meta->>'reason', meta->>'stop_reason')
                THEN 'timed_out' ELSE 'failed' END
        WHEN 'blocked' THEN 'skipped'
        WHEN 'timed_out' THEN 'timed_out'
    END;
END $$;

-- run_rows as in 024, except the task branch: its state comes from dispatch_state, and a
-- timed_out row counts as settled.
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
    NULL::text                                          AS failure_class,
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
