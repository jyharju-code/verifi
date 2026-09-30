-- Contract v3: three gates, euro pricing, EURC.
-- See docs/specs/extension-service-windows.md and docs/plans/contract-v3-three-gate.md.
--
-- Idempotent. Existing rows keep contract_version 2 and v2 semantics: nothing
-- here re-prices, re-times or rewrites a historical chain. Mirrors
-- core/db/pricing.sql, core/db/earnings.sql, and the v3 columns in
-- core/db/verifies.sql and core/db/associates.sql.

CREATE TABLE IF NOT EXISTS pricing_terms (
    terms_id     TEXT PRIMARY KEY,
    issued_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    valid_until  TIMESTAMPTZ NOT NULL,
    fingerprint  TEXT NOT NULL,
    terms        JSONB NOT NULL,
    internal     JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS pricing_terms_reuse_idx
    ON pricing_terms (fingerprint, valid_until DESC);

CREATE TABLE IF NOT EXISTS fx_rates (
    id          BIGSERIAL PRIMARY KEY,
    source      TEXT NOT NULL,
    base        CHAR(3) NOT NULL,
    quote       CHAR(3) NOT NULL,
    rate        NUMERIC(18,8) NOT NULL CHECK (rate > 0),
    rate_date   DATE NOT NULL,
    fetched_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source, base, quote, rate_date)
);

ALTER TABLE verifies ADD COLUMN IF NOT EXISTS contract_version SMALLINT NOT NULL DEFAULT 2;
ALTER TABLE verifies ADD COLUMN IF NOT EXISTS terms_id TEXT;
ALTER TABLE verifies ADD COLUMN IF NOT EXISTS bound_terms JSONB;
ALTER TABLE verifies ADD COLUMN IF NOT EXISTS bound_asset TEXT;
ALTER TABLE verifies ADD COLUMN IF NOT EXISTS bound_network TEXT;
ALTER TABLE verifies ADD COLUMN IF NOT EXISTS bound_commission_eur NUMERIC(10,2);
ALTER TABLE verifies ADD COLUMN IF NOT EXISTS entry_amount_atomic NUMERIC(30,0);
ALTER TABLE verifies ADD COLUMN IF NOT EXISTS entry_charged_atomic NUMERIC(30,0);
ALTER TABLE verifies ADD COLUMN IF NOT EXISTS sla_deadline TIMESTAMPTZ;
ALTER TABLE verifies ADD COLUMN IF NOT EXISTS grace_deadline TIMESTAMPTZ;
ALTER TABLE verifies ADD COLUMN IF NOT EXISTS ready_at TIMESTAMPTZ;
ALTER TABLE verifies ADD COLUMN IF NOT EXISTS applied_window TEXT;
ALTER TABLE verifies ADD COLUMN IF NOT EXISTS unlock_amount_atomic NUMERIC(30,0);
ALTER TABLE verifies ADD COLUMN IF NOT EXISTS unlock_charged_atomic NUMERIC(30,0);

DO $$ BEGIN
    ALTER TABLE verifies ADD CONSTRAINT verifies_contract_version_check
        CHECK (contract_version IN (2, 3));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
DO $$ BEGIN
    ALTER TABLE verifies ADD CONSTRAINT verifies_applied_window_check
        CHECK (applied_window IS NULL OR applied_window IN ('sla', 'grace'));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
DO $$ BEGIN
    ALTER TABLE verifies ADD CONSTRAINT verifies_v3_bound_check CHECK (
        contract_version = 2
        OR (terms_id IS NOT NULL AND bound_terms IS NOT NULL AND bound_asset IS NOT NULL)
    );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
DO $$ BEGIN
    ALTER TABLE verifies ADD CONSTRAINT verifies_terms_id_fkey
        FOREIGN KEY (terms_id) REFERENCES pricing_terms(terms_id);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

CREATE INDEX IF NOT EXISTS verifies_expiry_due_idx
    ON verifies (expires_at) WHERE status = 'pending';

ALTER TABLE payouts ADD COLUMN IF NOT EXISTS asset TEXT NOT NULL DEFAULT 'USDC';

CREATE TABLE IF NOT EXISTS associate_earnings (
    id             BIGSERIAL PRIMARY KEY,
    associate_id   INTEGER NOT NULL REFERENCES associates(id),
    verify_id      UUID REFERENCES verifies(id),
    kind           TEXT NOT NULL CHECK (kind IN ('answer', 'legacy_opening_balance')),
    asset          TEXT NOT NULL,
    amount_atomic  NUMERIC(30,0) NOT NULL CHECK (amount_atomic >= 0),
    decimals       SMALLINT NOT NULL CHECK (decimals BETWEEN 0 AND 18),
    applied_window TEXT CHECK (applied_window IS NULL OR applied_window IN ('sla', 'grace')),
    rule           TEXT NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS associate_earnings_verify_idx
    ON associate_earnings (verify_id) WHERE verify_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS associate_earnings_opening_idx
    ON associate_earnings (associate_id) WHERE kind = 'legacy_opening_balance';
CREATE INDEX IF NOT EXISTS associate_earnings_balance_idx
    ON associate_earnings (associate_id, asset, created_at DESC);

-- Carry every pre-v3 balance into the ledger exactly once, as USDC, which is
-- the only asset that existed. associates.earnings is not written after this.
INSERT INTO associate_earnings (associate_id, kind, asset, amount_atomic, decimals, rule)
SELECT id, 'legacy_opening_balance', 'USDC', round(earnings * 1000000), 6,
       'associates.earnings at the contract v3 migration'
FROM associates
WHERE earnings > 0
ON CONFLICT DO NOTHING;
