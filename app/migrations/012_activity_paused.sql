-- Activity: a paused job (plans/10 P1.7: a change job stopped by its timeout, tool cap or a
-- worker restart, its work kept on a draft PR, waiting to be resumed) is pending, not an error.
-- Same function as 008 apart from the paused lines.

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
                WHEN 'paused' THEN ' paused ('
                    || replace(coalesce(NEW.meta->>'pause_reason', '?'), '_', ' ') || ')'
                WHEN 'running' THEN ' started'
                WHEN 'succeeded' THEN ' done'
                ELSE ' ' || replace(NEW.status, '_', ' ') END || ': ' || NEW.title,
            CASE NEW.status WHEN 'succeeded' THEN 'ok' WHEN 'running' THEN 'running'
                            WHEN 'queued' THEN 'info' WHEN 'needs_approval' THEN 'pending'
                            WHEN 'needs_input' THEN 'pending'
                            WHEN 'paused' THEN 'pending'
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
