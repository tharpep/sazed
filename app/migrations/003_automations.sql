-- Automations v2: automations are data, not code. Created by chat (Sazed tools), the console,
-- or (later) MCP; run by the scheduler in app/automations/. See plans/06 step 5.

CREATE TABLE IF NOT EXISTS automations (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name          TEXT NOT NULL,
    description   TEXT NOT NULL DEFAULT '',
    enabled       BOOLEAN NOT NULL DEFAULT TRUE,
    -- {"type": "cron", "cron": "0 8 * * 1-5", "timezone": "America/New_York"}
    trigger       JSONB NOT NULL,
    -- {"type": "sazed_prompt", "prompt": "..."}
    -- {"type": "gateway_call", "method": "POST", "path": "/kb/sync", "body": {...}}
    action        JSONB NOT NULL,
    -- {"pushover": {"title": "...", "priority": 0}, "journal": {"category": "career",
    --  "subcategory": "..."}, "skip_if_nothing": true}
    outputs       JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_by    TEXT NOT NULL DEFAULT 'console',  -- console | sazed | claude
    next_run_at   TIMESTAMPTZ,                      -- NULL when disabled
    last_run_at   TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_automations_due
    ON automations (next_run_at) WHERE enabled;

CREATE TABLE IF NOT EXISTS automation_runs (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    automation_id  UUID NOT NULL REFERENCES automations(id) ON DELETE CASCADE,
    triggered_by   TEXT NOT NULL,           -- schedule | manual
    status         TEXT NOT NULL,           -- running | success | skipped | needs_approval | error | missed
    started_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at    TIMESTAMPTZ,
    output         TEXT,                    -- Sazed's reply or the gateway response (truncated)
    error          TEXT,
    session_id     UUID,                    -- the Sazed session a prompt ran in (open it in History)
    delivered      JSONB NOT NULL DEFAULT '[]'::jsonb  -- outputs that were sent, e.g. ["pushover"]
);

CREATE INDEX IF NOT EXISTS idx_automation_runs_automation
    ON automation_runs (automation_id, started_at DESC);
