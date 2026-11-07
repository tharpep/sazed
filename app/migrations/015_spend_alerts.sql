-- Monthly API spend alerts (app/spend_alert.py): one row per (month, threshold step) already
-- sent, so a check never repeats an alert within the month. A new month has no rows, which
-- is the reset.

CREATE TABLE IF NOT EXISTS spend_alerts (
    month      TEXT NOT NULL,          -- 'YYYY-MM' in the alert time zone
    pct        INT NOT NULL,           -- threshold step, percent of the monthly limit
    spend_usd  DOUBLE PRECISION NOT NULL,
    sent_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (month, pct)
);
