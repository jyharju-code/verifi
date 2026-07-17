-- Adds unlock and expiry fields agreed on 17.7.2026.
-- Idempotent: safe to run more than once.
ALTER TABLE verifies ADD COLUMN IF NOT EXISTS unlock_paid BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE verifies ADD COLUMN IF NOT EXISTS x402_payment_tx VARCHAR(80);
ALTER TABLE verifies ADD COLUMN IF NOT EXISTS x402_unlock_tx VARCHAR(80);
ALTER TABLE verifies ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;
UPDATE verifies SET expires_at = created_at + interval '60 minutes' WHERE expires_at IS NULL;
ALTER TABLE verifies ALTER COLUMN expires_at SET DEFAULT (now() + interval '60 minutes');
ALTER TABLE verifies ALTER COLUMN expires_at SET NOT NULL;
-- Existing rows: everything answered so far has been visible, keep it that way.
UPDATE verifies SET unlock_paid = true;
