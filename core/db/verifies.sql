-- Verifies: one row per verification request from an agent.
--
-- Additions beyond CLAUDE.md schema, documented in docs/DECISIONS.md:
--   verify_no           short human-readable number for Telegram (#V-1042)
--   assigned_at         when the verify was routed to an associate
--   telegram_message_id maps Telegram button taps and refine replies back to the row
--   status 'expired'    no associate answered within the instance timeout

CREATE TABLE IF NOT EXISTS verifies (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    verify_no           SERIAL,
    instance            VARCHAR(50) NOT NULL REFERENCES instances(id),
    intent              TEXT NOT NULL,
    claim               TEXT NOT NULL,
    agent_id            VARCHAR(100),
    -- free means one of the five full-chain entitlements. paid also covers
    -- an entry funded by a failure credit, because its result still costs.
    tier                VARCHAR(10) NOT NULL CHECK (tier IN ('free', 'paid')),
    associate_id        INTEGER REFERENCES associates(id),
    status              VARCHAR(20) NOT NULL DEFAULT 'admission_pending'
                        CHECK (status IN ('admission_pending', 'pending', 'accepted',
                                         'rejected', 'refined', 'expired', 'failed')),
    response            TEXT,
    response_time_ms    INTEGER,
    entry_source        VARCHAR(20) CHECK (entry_source IN
                        ('initial_free', 'failure_credit', 'x402')),
    entry_list_price_usdc NUMERIC(10,2) NOT NULL DEFAULT 0.10,
    entry_charged_usdc  NUMERIC(10,2) NOT NULL DEFAULT 0.00,
    entry_payer         VARCHAR(80),
    unlock_source       VARCHAR(20) CHECK (unlock_source IN ('initial_free', 'x402')),
    unlock_list_price_usdc NUMERIC(10,2) NOT NULL DEFAULT 2.90,
    unlock_charged_usdc NUMERIC(10,2) NOT NULL DEFAULT 0.00,
    unlock_payer        VARCHAR(80),
    result_unlocked     BOOLEAN NOT NULL DEFAULT false,
    free_use_number     SMALLINT,
    failure_credit_granted BOOLEAN NOT NULL DEFAULT false,
    x402_payment_tx     VARCHAR(80),
    x402_unlock_tx      VARCHAR(80),
    admitted_at         TIMESTAMPTZ,
    unlocked_at         TIMESTAMPTZ,
    failure_reason      TEXT,
    telegram_message_id BIGINT,
    assigned_at         TIMESTAMPTZ,
    callback_url        TEXT,
    callback_delivered  BOOLEAN NOT NULL DEFAULT false,
    callback_attempts   INTEGER NOT NULL DEFAULT 0,
    callback_last_attempt TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at          TIMESTAMPTZ NOT NULL DEFAULT (now() + interval '60 minutes'),
    responded_at        TIMESTAMPTZ,
    -- nginx-generated id of the request that created this verify. Ties the row
    -- to its nginx JSON access log line and its request_audit row.
    request_id          TEXT,
    -- Contract v3 (docs/specs/extension-service-windows.md). Rows created
    -- before v3 keep contract_version 2 and v2 semantics, and these stay null.
    contract_version    SMALLINT NOT NULL DEFAULT 2 CHECK (contract_version IN (2, 3)),
    terms_id            TEXT REFERENCES pricing_terms(terms_id),
    -- The quote as bound at admission for the asset actually paid: windows,
    -- basis prices, asset, and its admission, sla and grace amounts. Never
    -- changed afterwards.
    bound_terms         JSONB,
    bound_asset         TEXT,
    bound_network       TEXT,
    bound_commission_eur NUMERIC(10,2),
    entry_amount_atomic NUMERIC(30,0),
    entry_charged_atomic NUMERIC(30,0),
    sla_deadline        TIMESTAMPTZ,
    grace_deadline      TIMESTAMPTZ,
    -- When the human's answer was recorded. The only input to the unlock price.
    -- responded_at is not reused: it is also written on expiry.
    ready_at            TIMESTAMPTZ,
    applied_window      TEXT CHECK (applied_window IS NULL OR applied_window IN ('sla', 'grace')),
    -- Decided once at ready_at from the bound amounts, then only read.
    unlock_amount_atomic NUMERIC(30,0),
    unlock_charged_atomic NUMERIC(30,0),
    CONSTRAINT verifies_v3_bound_check CHECK (
        contract_version = 2
        OR (terms_id IS NOT NULL AND bound_terms IS NOT NULL AND bound_asset IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS verifies_instance_status_idx ON verifies (instance, status);
CREATE INDEX IF NOT EXISTS verifies_associate_idx ON verifies (associate_id, created_at DESC);
CREATE INDEX IF NOT EXISTS verifies_agent_idx ON verifies (agent_id);
CREATE INDEX IF NOT EXISTS verifies_tg_msg_idx ON verifies (associate_id, telegram_message_id);
CREATE INDEX IF NOT EXISTS verifies_wallet_idx ON verifies (instance, lower(agent_id), created_at DESC);
CREATE INDEX IF NOT EXISTS verifies_request_id_idx ON verifies (request_id);
CREATE INDEX IF NOT EXISTS verifies_callback_due_idx
    ON verifies (callback_delivered, status)
    WHERE callback_url IS NOT NULL;
