-- Activity: a job waiting on Pryce's answer (status needs_input, plans/11 job controls) is
-- pending, not an error. 006's job trigger mapped every status it didn't list to 'error'.
-- Same function as 006 apart from the needs_input lines.

CREATE OR REPLACE FUNCTION activity_on_job() RETURNS trigger AS $$
DECLARE
    tr TEXT;
    refs JSONB;
    label TEXT;
BEGIN
    -- Assigned here, not in DECLARE: initializer errors would escape the EXCEPTION clause.
    tr := activity_job_trace(NEW.id);
    refs := jsonb_build_object('job', NEW.id, 'parent', NEW.parent_id, 'agent', NEW.agent,
                               'model', NEW.model, 'requested_by', NEW.requested_by,
                               'repo', NEW.params->>'repo');
    label := activity_job_label(NEW.kind);
    IF TG_OP = 'INSERT' OR NEW.status IS DISTINCT FROM OLD.status THEN
        PERFORM activity_emit(
            'job',
            label || CASE NEW.status
                WHEN 'queued' THEN ' queued'
                WHEN 'needs_approval' THEN ' waiting for approval'
                WHEN 'needs_input' THEN ' waiting for your answer'
                WHEN 'running' THEN ' started'
                WHEN 'succeeded' THEN ' done'
                ELSE ' ' || replace(NEW.status, '_', ' ') END || ': ' || NEW.title,
            CASE NEW.status WHEN 'succeeded' THEN 'ok' WHEN 'running' THEN 'running'
                            WHEN 'queued' THEN 'info' WHEN 'needs_approval' THEN 'pending'
                            WHEN 'needs_input' THEN 'pending'
                            WHEN 'cancelled' THEN 'info' ELSE 'error' END,
            tr, refs,
            jsonb_build_object('error', NEW.error, 'result', left(NEW.result, 1500),
                               'meta', NEW.meta - 'usage'),
            CASE WHEN NEW.finished_at IS NOT NULL AND NEW.started_at IS NOT NULL
                 THEN (extract(epoch FROM (NEW.finished_at - NEW.started_at)) * 1000)::int END);
    END IF;
    IF TG_OP = 'UPDATE' AND NEW.meta->>'pr_url' IS DISTINCT FROM OLD.meta->>'pr_url'
       AND NEW.meta->>'pr_url' IS NOT NULL THEN
        PERFORM activity_emit(
            'job.pr',
            'PR #' || (NEW.meta->>'pr_number') ||
                CASE WHEN (NEW.meta->>'pr_updated')::boolean THEN ' updated' ELSE ' opened' END ||
                ' · ' || coalesce(split_part(NEW.params->>'repo', '/', 2), ''),
            'ok', tr, refs || jsonb_build_object('pr_url', NEW.meta->>'pr_url'),
            jsonb_build_object('pr_url', NEW.meta->>'pr_url'), NULL);
    END IF;
    IF TG_OP = 'UPDATE' AND NEW.meta->>'ci_state' IS DISTINCT FROM OLD.meta->>'ci_state'
       AND NEW.meta->>'ci_state' IS NOT NULL THEN
        PERFORM activity_emit(
            'job.ci',
            'CI ' || replace(NEW.meta->>'ci_state', '_', ' ') || ' on PR #' ||
                coalesce(NEW.meta->>'pr_number', '?'),
            CASE NEW.meta->>'ci_state' WHEN 'success' THEN 'ok' WHEN 'failure' THEN 'error'
                                       WHEN 'fixing' THEN 'running' ELSE 'info' END,
            tr, refs || jsonb_build_object('pr_url', NEW.meta->>'pr_url'),
            jsonb_build_object('ci_runs', NEW.meta->'ci_runs'), NULL);
    END IF;
    RETURN NEW;
EXCEPTION WHEN OTHERS THEN
    RAISE WARNING 'activity_on_job: %', SQLERRM;
    RETURN NEW;
END $$ LANGUAGE plpgsql;

-- Events already recorded under the old mapping (the first needs_input job, 2026-10-27).
-- Guarded like 006: on GCP the shared migration counter can mean 006 never ran.
DO $$
BEGIN
    IF to_regclass('activity_events') IS NULL THEN
        RETURN;
    END IF;
    UPDATE activity_events
    SET status = 'pending',
        title = replace(title, ' needs input: ', ' waiting for your answer: ')
    WHERE kind = 'job' AND status = 'error' AND title LIKE '% needs input: %';
END $$;
