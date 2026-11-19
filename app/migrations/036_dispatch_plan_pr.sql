-- One stack, seam 6: the draft PR an unattended export opened for a plan, through the gateway's
-- bot. A re-export names pr_number so the gateway updates that PR and never reopens a closed one.
-- Each column is guarded, so a second run changes nothing.
ALTER TABLE dispatch_plans
    ADD COLUMN IF NOT EXISTS pr_url TEXT,
    ADD COLUMN IF NOT EXISTS pr_number INT,
    ADD COLUMN IF NOT EXISTS pr_head_sha TEXT,
    ADD COLUMN IF NOT EXISTS exported_at TIMESTAMPTZ;
