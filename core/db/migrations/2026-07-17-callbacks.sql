-- Optional webhook delivery for resolved verifies. Idempotent.
ALTER TABLE verifies ADD COLUMN IF NOT EXISTS callback_url TEXT;
ALTER TABLE verifies ADD COLUMN IF NOT EXISTS callback_delivered BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE verifies ADD COLUMN IF NOT EXISTS callback_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE verifies ADD COLUMN IF NOT EXISTS callback_last_attempt TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS verifies_callback_due_idx
    ON verifies (callback_delivered, status)
    WHERE callback_url IS NOT NULL;
