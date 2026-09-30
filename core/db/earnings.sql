-- Responder earnings ledger, append-only, one row per answered verify.
--
-- Earnings are recorded in the asset the chain is bound to and never
-- converted: a USDC chain earns USDC, an EURC chain earns EURC. The amount
-- follows the unlock price of the window the answer fell into (contract v3,
-- D11), so a grace-window answer earns from the grace price.
--
-- associates.earnings predates this ledger. The v3 migration copies it once
-- into a legacy_opening_balance row (USDC), after which balances come only
-- from this ledger minus payouts in the same asset.

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
