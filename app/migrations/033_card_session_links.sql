-- A terminal session building a card links itself here: a free-text label, a branch, and the PR.
-- A link puts the card in "doing"; the PR merging closes it (intake handles pull_request closed).
ALTER TABLE backlog_items
    ADD COLUMN session_label  TEXT,
    ADD COLUMN session_branch TEXT,
    ADD COLUMN pr_repo        TEXT,
    ADD COLUMN pr_number      INT;

CREATE INDEX idx_backlog_linked_pr ON backlog_items (lower(pr_repo), pr_number)
    WHERE pr_number IS NOT NULL;
