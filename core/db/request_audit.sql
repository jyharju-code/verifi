-- Durable request audit trail.
--
-- One row per money- or human-facing request (REST /verify and /verify-unlock,
-- MCP tool calls, and the website contact form). It survives container
-- recreation because it lives in PostgreSQL, unlike the nginx access log. The
-- shared request_id column ties a row to its nginx JSON access log line and,
-- when a verification is created, to the verifies row.
--
-- Trust and privacy boundaries, enforced by the writers, not by this schema:
--   * client_ip is the direct TCP peer as seen by nginx (X-Real-IP), never a
--     value taken from an untrusted X-Forwarded-For header.
--   * forwarded_for stores the raw X-Forwarded-For only as an untrusted hint.
--     forwarded_trusted is true only when the request arrived from a
--     configured local or Tailscale proxy.
--   * agent_id is the wallet address the caller CLAIMED. It is not proof of
--     anything. wallet_ownership_proven is true only when a valid x402
--     payment signature for this request was verified, which is the only
--     cryptographic proof the caller controls that wallet.
--   * claim_sha256 and claim_len are the ONLY claim-derived data allowed here.
--     The claim text itself lives in verifies, never in this table.
--
-- Never written here: Authorization, PAYMENT-SIGNATURE, PAYMENT-RESPONSE,
-- cookies, private keys, .env contents, x402 signatures, Telegram tokens, DB
-- passwords, full callback URLs (host only), intent or claim text, or any
-- request or response body.
--
-- IP addresses are personal data. Retention of this table is governed by
-- operational policy alongside the nginx logs (see docs/OBSERVABILITY.md).

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
