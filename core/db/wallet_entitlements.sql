-- Transparent wallet entitlement ledger.
--
-- initial_free rows represent one of the five complete free chains. They
-- cover both the 0.10 USDC entry gate and the 2.90 USDC result gate.
-- failure_credit rows are granted by a failed admitted verify and cover only
-- the next 0.10 USDC entry gate. Rows are never deleted.

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
