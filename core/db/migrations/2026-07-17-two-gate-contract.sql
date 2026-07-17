-- Two-gate Verifi contract: 0.10 USDC admission, then 2.90 USDC unlock.
-- The first five complete chains per wallet are free. Failed admitted chains
-- grant one entry-only credit. Idempotent and safe to run more than once.

ALTER TABLE verifies DROP CONSTRAINT IF EXISTS verifies_status_check;
ALTER TABLE verifies ADD CONSTRAINT verifies_status_check
    CHECK (status IN ('admission_pending', 'pending', 'accepted', 'rejected',
                      'refined', 'expired', 'failed'));

ALTER TABLE verifies ADD COLUMN IF NOT EXISTS entry_source VARCHAR(20);
ALTER TABLE verifies ADD COLUMN IF NOT EXISTS entry_list_price_usdc NUMERIC(10,2) NOT NULL DEFAULT 0.10;
ALTER TABLE verifies ADD COLUMN IF NOT EXISTS entry_charged_usdc NUMERIC(10,2) NOT NULL DEFAULT 0.00;
ALTER TABLE verifies ADD COLUMN IF NOT EXISTS entry_payer VARCHAR(80);
ALTER TABLE verifies ADD COLUMN IF NOT EXISTS unlock_source VARCHAR(20);
ALTER TABLE verifies ADD COLUMN IF NOT EXISTS unlock_list_price_usdc NUMERIC(10,2) NOT NULL DEFAULT 2.90;
ALTER TABLE verifies ADD COLUMN IF NOT EXISTS unlock_charged_usdc NUMERIC(10,2) NOT NULL DEFAULT 0.00;
ALTER TABLE verifies ADD COLUMN IF NOT EXISTS unlock_payer VARCHAR(80);
ALTER TABLE verifies ADD COLUMN IF NOT EXISTS result_unlocked BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE verifies ADD COLUMN IF NOT EXISTS free_use_number SMALLINT;
ALTER TABLE verifies ADD COLUMN IF NOT EXISTS failure_credit_granted BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE verifies ADD COLUMN IF NOT EXISTS admitted_at TIMESTAMPTZ;
ALTER TABLE verifies ADD COLUMN IF NOT EXISTS unlocked_at TIMESTAMPTZ;
ALTER TABLE verifies ADD COLUMN IF NOT EXISTS failure_reason TEXT;

DO $$ BEGIN
    ALTER TABLE verifies ADD CONSTRAINT verifies_entry_source_check
        CHECK (entry_source IS NULL OR entry_source IN ('initial_free', 'failure_credit', 'x402'));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
DO $$ BEGIN
    ALTER TABLE verifies ADD CONSTRAINT verifies_unlock_source_check
        CHECK (unlock_source IS NULL OR unlock_source IN ('initial_free', 'x402'));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

UPDATE instances
SET price_per_verify = 3.00, free_tier_count = 5
WHERE id = 'verify-api';

-- Preserve the historical record. Old free answers were already delivered,
-- while old paid 0.10 payments become entry payments and their answers now
-- wait for the separate 2.90 unlock unless a historical unlock exists.
UPDATE verifies
SET entry_source = CASE WHEN tier = 'free' THEN 'initial_free' ELSE 'x402' END,
    entry_charged_usdc = CASE
        WHEN tier = 'paid' AND x402_payment_tx IS NOT NULL THEN 0.10 ELSE 0.00 END,
    entry_payer = COALESCE(entry_payer, agent_id),
    admitted_at = COALESCE(admitted_at, created_at),
    result_unlocked = CASE
        WHEN tier = 'free' THEN true
        WHEN x402_unlock_tx IS NOT NULL THEN true
        ELSE false END,
    unlock_source = CASE
        WHEN tier = 'free' THEN 'initial_free'
        WHEN x402_unlock_tx IS NOT NULL THEN 'x402'
        ELSE NULL END,
    unlock_charged_usdc = CASE WHEN x402_unlock_tx IS NOT NULL THEN 3.00 ELSE 0.00 END,
    unlocked_at = CASE
        WHEN tier = 'free' OR x402_unlock_tx IS NOT NULL THEN COALESCE(responded_at, created_at)
        ELSE NULL END
WHERE entry_source IS NULL;

WITH ranked AS (
    SELECT id, instance, agent_id,
           row_number() OVER (PARTITION BY instance, lower(agent_id) ORDER BY created_at, id) AS n
    FROM verifies
    WHERE tier = 'free' AND agent_id IS NOT NULL
)
UPDATE verifies v
SET free_use_number = ranked.n
FROM ranked
WHERE v.id = ranked.id AND v.free_use_number IS NULL;

CREATE TABLE IF NOT EXISTS wallet_entitlements (
    id                  BIGSERIAL PRIMARY KEY,
    instance            VARCHAR(50) NOT NULL REFERENCES instances(id),
    wallet_address      VARCHAR(42) NOT NULL,
    kind                VARCHAR(30) NOT NULL
                        CHECK (kind IN ('initial_free', 'failure_credit')),
    covers_entry        BOOLEAN NOT NULL DEFAULT true,
    covers_unlock       BOOLEAN NOT NULL DEFAULT false,
    free_use_number     SMALLINT,
    source_verify_id    UUID REFERENCES verifies(id),
    consumed_by_verify_id UUID REFERENCES verifies(id),
    granted_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    consumed_at         TIMESTAMPTZ,
    details             JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE UNIQUE INDEX IF NOT EXISTS wallet_entitlements_initial_free_idx
    ON wallet_entitlements (instance, lower(wallet_address), free_use_number)
    WHERE kind = 'initial_free';
CREATE UNIQUE INDEX IF NOT EXISTS wallet_entitlements_failure_source_idx
    ON wallet_entitlements (source_verify_id)
    WHERE kind = 'failure_credit';
CREATE UNIQUE INDEX IF NOT EXISTS wallet_entitlements_consumed_idx
    ON wallet_entitlements (consumed_by_verify_id)
    WHERE consumed_by_verify_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS wallet_entitlements_available_idx
    ON wallet_entitlements (instance, lower(wallet_address), kind, granted_at)
    WHERE consumed_by_verify_id IS NULL;

INSERT INTO wallet_entitlements (
    instance, wallet_address, kind, covers_entry, covers_unlock,
    free_use_number, consumed_by_verify_id, granted_at, consumed_at,
    details
)
SELECT instance, agent_id, 'initial_free', true, true,
       free_use_number, id, created_at, created_at,
       jsonb_build_object('migrated', true)
FROM verifies
WHERE tier = 'free' AND agent_id IS NOT NULL AND free_use_number IS NOT NULL
ON CONFLICT DO NOTHING;

INSERT INTO wallet_entitlements (
    instance, wallet_address, kind, covers_entry, covers_unlock,
    source_verify_id, granted_at, details
)
SELECT instance, agent_id, 'failure_credit', true, false,
       id, COALESCE(responded_at, expires_at, created_at),
       jsonb_build_object('reason', 'expired', 'migrated', true)
FROM verifies
WHERE status = 'expired' AND agent_id IS NOT NULL
ON CONFLICT DO NOTHING;

UPDATE verifies v
SET failure_credit_granted = true
WHERE status = 'expired' AND EXISTS (
    SELECT 1 FROM wallet_entitlements e
    WHERE e.kind = 'failure_credit' AND e.source_verify_id = v.id
);

CREATE INDEX IF NOT EXISTS verifies_wallet_idx
    ON verifies (instance, lower(agent_id), created_at DESC);
