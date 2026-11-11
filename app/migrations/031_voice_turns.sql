-- One row per voice turn: where the time went between the user finishing speaking and hearing the reply.
CREATE TABLE IF NOT EXISTS voice_turns (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id          UUID REFERENCES sessions(id) ON DELETE SET NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    kiosk               BOOLEAN NOT NULL DEFAULT FALSE,
    outcome             TEXT NOT NULL
        CHECK (outcome IN ('spoken', 'silent', 'interrupted', 'stopped', 'error')),
    tool_turn           BOOLEAN NOT NULL DEFAULT FALSE,
    engine              TEXT,
    utterance_ms        INT,
    endpoint_ms         INT,
    stt_ms              INT,
    setup_ms            INT,
    llm_ttft_ms         INT,
    first_text_ms       INT,
    tts_first_byte_ms   INT,
    tts_first_sound_ms  INT,
    first_sound_ms      INT
);

CREATE INDEX IF NOT EXISTS voice_turns_created_idx ON voice_turns (created_at DESC);
