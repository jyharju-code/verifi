-- Durable request audit trail plus a request_id on verifies for correlation.
-- Idempotent. The full table definition and its trust/privacy rules live in
-- core/db/request_audit.sql; this migration brings an existing database up to
-- that shape.

CREATE TABLE IF NOT EXISTS request_audit (
    id                      BIGSERIAL PRIMARY KEY,
    at                      TIMESTAMPTZ NOT NULL DEFAULT now(),
    request_id              TEXT,
    source                  VARCHAR(16) NOT NULL
                            CHECK (source IN ('rest', 'mcp', 'website')),
    route                   TEXT,
    client_ip               INET,
    forwarded_for           TEXT,
    forwarded_trusted       BOOLEAN NOT NULL DEFAULT false,
    user_agent              TEXT,
    agent_id                VARCHAR(100),
    verify_id               UUID,
    verify_no               INTEGER,
    callback_host           TEXT,
    admission_source        VARCHAR(20),
    http_status             INTEGER,
    payment_required        BOOLEAN,
    funding_state           VARCHAR(24),
    outcome                 VARCHAR(32),
    wallet_ownership_proven BOOLEAN NOT NULL DEFAULT false,
    claim_len               INTEGER,
    claim_sha256            CHAR(64)
);

CREATE INDEX IF NOT EXISTS request_audit_request_id_idx ON request_audit (request_id);
CREATE INDEX IF NOT EXISTS request_audit_verify_id_idx ON request_audit (verify_id);
CREATE INDEX IF NOT EXISTS request_audit_verify_no_idx ON request_audit (verify_no);
CREATE INDEX IF NOT EXISTS request_audit_agent_idx ON request_audit (lower(agent_id), at DESC);
CREATE INDEX IF NOT EXISTS request_audit_at_idx ON request_audit (at DESC);

-- Correlate a verification back to the request that created it.
ALTER TABLE verifies ADD COLUMN IF NOT EXISTS request_id TEXT;
CREATE INDEX IF NOT EXISTS verifies_request_id_idx ON verifies (request_id);
