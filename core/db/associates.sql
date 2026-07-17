-- Associates: the humans who answer verifies through the Telegram bot.
--
-- Additions beyond CLAUDE.md schema, documented in docs/PAATOKSET.md:
--   username         Telegram @username, needed by /lisaa and /poista matching
--   available        /vapaa and /varattu toggle (status is the admin-level switch)
--   paid_total       lifetime paid out, pending balance = earnings - paid_total
--   last_assigned_at round-robin tiebreak for routing
--   status 'pending' self-registered via /start, not yet approved with /lisaa

CREATE TABLE IF NOT EXISTS associates (
    id               SERIAL PRIMARY KEY,
    name             VARCHAR(200) NOT NULL,
    username         VARCHAR(100),
    telegram_id      BIGINT NOT NULL UNIQUE,
    wallet_address   VARCHAR(42) CHECK (wallet_address IS NULL OR wallet_address ~ '^0x[0-9a-fA-F]{40}$'),
    payout_method    VARCHAR(10) NOT NULL DEFAULT 'bank' CHECK (payout_method IN ('crypto', 'bank')),
    status           VARCHAR(20) NOT NULL DEFAULT 'pending'
                     CHECK (status IN ('pending', 'active', 'paused', 'removed')),
    available        BOOLEAN NOT NULL DEFAULT FALSE,
    instance_active  VARCHAR(50) REFERENCES instances(id),
    total_free       INTEGER NOT NULL DEFAULT 0,
    total_paid       INTEGER NOT NULL DEFAULT 0,
    earnings         NUMERIC(10,2) NOT NULL DEFAULT 0,
    paid_total       NUMERIC(10,2) NOT NULL DEFAULT 0,
    accuracy         NUMERIC(5,2) NOT NULL DEFAULT 1.00 CHECK (accuracy >= 0 AND accuracy <= 1),
    last_assigned_at TIMESTAMPTZ,
    joined_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS associates_routing_idx
    ON associates (status, available, instance_active);
CREATE INDEX IF NOT EXISTS associates_username_idx
    ON associates (lower(username));

-- Payout log: one row per completed payout (crypto tx or manual bank transfer).
CREATE TABLE IF NOT EXISTS payouts (
    id           SERIAL PRIMARY KEY,
    associate_id INTEGER NOT NULL REFERENCES associates(id),
    amount       NUMERIC(10,2) NOT NULL CHECK (amount > 0),
    method       VARCHAR(10) NOT NULL CHECK (method IN ('crypto', 'bank')),
    tx_reference VARCHAR(200),
    note         TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS payouts_associate_idx ON payouts (associate_id, created_at DESC);
