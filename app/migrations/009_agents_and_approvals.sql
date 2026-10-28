-- plans/12 phase 1: agents run on the step runner (app/agents), with a per-step run record and
-- one approvals table for steps that wait on Pryce.

-- Automations of type 'pipeline' become 'agent'; the scout keeps max_ideas as a param.
UPDATE automations
SET action = jsonb_build_object(
        'type', 'agent',
        'name', action->>'name',
        'params', CASE WHEN action->>'name' = 'scout' AND action ? 'max_ideas'
                       THEN jsonb_build_object('max_ideas', action->'max_ideas')
                       ELSE '{}'::jsonb END)
WHERE action->>'type' = 'pipeline';

-- One entry per step: name, kind, status, duration_ms, detail, job, tokens, tool_calls.
ALTER TABLE automation_runs ADD COLUMN IF NOT EXISTS steps JSONB NOT NULL DEFAULT '[]'::jsonb;

-- Things waiting on Pryce. Phase 1: an agent's pause step (kind 'agent_step'); chat's
-- pending_actions move here in phase 2.
CREATE TABLE IF NOT EXISTS approvals (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    kind          TEXT NOT NULL,                       -- agent_step
    title         TEXT NOT NULL,                       -- the question Pryce answers
    requested_by  TEXT NOT NULL,                       -- automation-run:<id>
    payload       JSONB NOT NULL DEFAULT '{}'::jsonb,  -- what resumes on approve
    status        TEXT NOT NULL DEFAULT 'pending',     -- pending | approved | denied | expired
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at    TIMESTAMPTZ NOT NULL DEFAULT NOW() + INTERVAL '3 days',
    decided_at    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS approvals_pending_idx ON approvals (created_at) WHERE status = 'pending';

CREATE OR REPLACE FUNCTION activity_on_approval() RETURNS trigger AS $$
DECLARE
    tr TEXT;
BEGIN
    -- Assigned here, not in DECLARE: initializer errors would escape the EXCEPTION clause.
    tr := CASE WHEN NEW.requested_by LIKE 'automation-run:%'
               THEN 'run:' || split_part(NEW.requested_by, ':', 2) END;
    IF TG_OP = 'INSERT' OR NEW.status IS DISTINCT FROM OLD.status THEN
        PERFORM activity_emit(
            'approval',
            CASE NEW.status
                WHEN 'pending' THEN 'Waiting for approval: '
                WHEN 'approved' THEN 'Approved: '
                WHEN 'denied' THEN 'Declined: '
                WHEN 'expired' THEN 'Approval expired: '
                ELSE NEW.status || ': ' END || NEW.title,
            CASE NEW.status WHEN 'pending' THEN 'pending' WHEN 'approved' THEN 'ok'
                            WHEN 'denied' THEN 'info' WHEN 'expired' THEN 'info'
                            ELSE 'error' END,
            tr,
            jsonb_build_object('approval', NEW.id, 'kind', NEW.kind,
                               'requested_by', NEW.requested_by),
            '{}'::jsonb, NULL);
    END IF;
    RETURN NEW;
EXCEPTION WHEN OTHERS THEN
    RAISE WARNING 'activity_on_approval: %', SQLERRM;
    RETURN NEW;
END $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS activity_approval_trg ON approvals;
CREATE TRIGGER activity_approval_trg AFTER INSERT OR UPDATE OF status ON approvals
    FOR EACH ROW EXECUTE FUNCTION activity_on_approval();
