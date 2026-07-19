-- Idempotent migration: independent on-chain confirmation for settlements.
-- Mirrors core/db/settlement_journal.sql. Safe to run more than once.

-- Independent on-chain confirmation. The x402 middleware and the facilitator
-- are the payment gate; these columns record that core also checked the hash
-- against Base itself, so a settlement that was never mined cannot sit in the
-- ledger unnoticed. NULL means not checked yet.
ALTER TABLE settlement_journal ADD COLUMN IF NOT EXISTS chain_verified BOOLEAN;
ALTER TABLE settlement_journal ADD COLUMN IF NOT EXISTS chain_checked_at TIMESTAMPTZ;
ALTER TABLE settlement_journal ADD COLUMN IF NOT EXISTS chain_detail TEXT;

CREATE INDEX IF NOT EXISTS settlement_journal_unverified_idx
    ON settlement_journal (created_at)
    WHERE applied = true AND chain_verified IS NULL;

-- Backfill settlements that predate the journal so they are confirmed against
-- the chain too. Idempotent: the unique (transaction, kind) index absorbs it.
INSERT INTO settlement_journal (verify_id, kind, transaction, payer, applied, applied_at)
SELECT id, 'entry', x402_payment_tx, entry_payer, true, COALESCE(admitted_at, created_at)
FROM verifies WHERE x402_payment_tx IS NOT NULL
ON CONFLICT (transaction, kind) DO NOTHING;

INSERT INTO settlement_journal (verify_id, kind, transaction, payer, applied, applied_at)
SELECT id, 'unlock', x402_unlock_tx, unlock_payer, true, COALESCE(unlocked_at, responded_at, created_at)
FROM verifies WHERE x402_unlock_tx IS NOT NULL
ON CONFLICT (transaction, kind) DO NOTHING;
