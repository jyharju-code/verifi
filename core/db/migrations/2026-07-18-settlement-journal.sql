-- Idempotent migration: adds the durable settlement journal (see
-- core/db/settlement_journal.sql). Safe to run more than once.
-- Durable write-ahead journal for x402 settlements.
--
-- The instance server reports a settled entry or unlock transaction to the
-- core /internal payment endpoint. The report is journaled here first, then
-- applied to the verify row. If the process dies between the two, or the
-- verify is not yet in an applicable state, the reconciliation loop retries
-- from this journal. That turns a lost settlement record from a silent
-- stderr line into a durable, auditable, self-healing state.

CREATE TABLE IF NOT EXISTS settlement_journal (
    id            BIGSERIAL PRIMARY KEY,
    verify_id     UUID NOT NULL REFERENCES verifies(id),
    kind          VARCHAR(10) NOT NULL CHECK (kind IN ('entry', 'unlock')),
    transaction   VARCHAR(80) NOT NULL,
    payer         VARCHAR(80),
    applied       BOOLEAN NOT NULL DEFAULT false,
    attempts      INTEGER NOT NULL DEFAULT 0,
    last_error    TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    applied_at    TIMESTAMPTZ
);

CREATE UNIQUE INDEX IF NOT EXISTS settlement_journal_tx_kind_idx
    ON settlement_journal (transaction, kind);
CREATE INDEX IF NOT EXISTS settlement_journal_unapplied_idx
    ON settlement_journal (created_at)
    WHERE applied = false;
