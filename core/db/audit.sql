-- Append-only audit log. Every money-related and state-changing event
-- lands here: verify lifecycle, price changes, payouts, settlements.
-- Rows are never updated or deleted by application code.

CREATE TABLE IF NOT EXISTS audit_log (
    id      BIGSERIAL PRIMARY KEY,
    at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    source  VARCHAR(50) NOT NULL,
    event   VARCHAR(100) NOT NULL,
    actor   VARCHAR(100),
    details JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS audit_log_at_idx ON audit_log (at DESC);
CREATE INDEX IF NOT EXISTS audit_log_event_idx ON audit_log (event, at DESC);
