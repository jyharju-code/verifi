-- Verifies: one row per verification request from an agent.
--
-- Additions beyond CLAUDE.md schema, documented in docs/PAATOKSET.md:
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
    tier                VARCHAR(10) NOT NULL CHECK (tier IN ('free', 'paid')),
    associate_id        INTEGER REFERENCES associates(id),
    status              VARCHAR(20) NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'accepted', 'rejected', 'refined', 'expired')),
    response            TEXT,
    response_time_ms    INTEGER,
    -- Paid responses auto-unlock when the x402 payment settles. The flag can
    -- only be false on a paid verify whose settlement failed after creation;
    -- the $3 unlock endpoint is the recovery path (docs/PAATOKSET.md).
    unlock_paid         BOOLEAN NOT NULL DEFAULT false,
    x402_payment_tx     VARCHAR(80),
    x402_unlock_tx      VARCHAR(80),
    telegram_message_id BIGINT,
    assigned_at         TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at          TIMESTAMPTZ NOT NULL DEFAULT (now() + interval '60 minutes'),
    responded_at        TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS verifies_instance_status_idx ON verifies (instance, status);
CREATE INDEX IF NOT EXISTS verifies_associate_idx ON verifies (associate_id, created_at DESC);
CREATE INDEX IF NOT EXISTS verifies_agent_idx ON verifies (agent_id);
CREATE INDEX IF NOT EXISTS verifies_tg_msg_idx ON verifies (associate_id, telegram_message_id);
