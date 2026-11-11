-- ms from the user finishing speaking to the filler clip starting; NULL when no filler played.
ALTER TABLE voice_turns ADD COLUMN filler_ms int;
