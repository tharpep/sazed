-- plans/10 P1.3: one build queue, claimed by priority, then age. Lower runs first:
-- 0 for jobs Pryce starts (console, his chats, MCP, backlog Approve), 5 for jobs Sazed starts
-- itself (automations, think runs, CI fix-ups); autopilot will use 9 (plans/10 A1).
-- store.claim_priority sets it on new jobs; this backfills the same rule for existing rows.

ALTER TABLE jobs ADD COLUMN IF NOT EXISTS priority INT NOT NULL DEFAULT 5;

UPDATE jobs SET priority = 0
WHERE priority <> 0
  AND NOT (requested_by LIKE 'automation%' OR requested_by LIKE 'think:%'
           OR requested_by LIKE 'ci-fix%' OR requested_by LIKE 'sazed%');

CREATE INDEX IF NOT EXISTS jobs_claim_idx ON jobs (priority, created_at) WHERE status = 'queued';
