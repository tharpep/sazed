-- Persists each session's selected tool-schema set so it stays stable across
-- turns instead of being recomputed from scratch per message. Anthropic's
-- prompt cache prefix is tools -> system -> messages, so a tool list that
-- changes shape on nearly every turn was invalidating the cache for the
-- (otherwise byte-identical) system prompt and memory block too. Growing this
-- append-only per session restores cache hits across a conversation.
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS selected_tools TEXT[];
