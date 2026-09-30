-- Quotes and exchange rates for contract v3 (docs/specs/extension-service-windows.md).
--
-- pricing_terms holds one row per quote a 402 advertised. The public `terms`
-- object is exactly the service-windows `info` an agent sees. `internal` holds
-- what the servers need but agents never see: asset metadata for building x402
-- requirements, and the responder commission snapshotted for earnings.
-- A work item binds to its quote at admission and never reads it again.
--
-- fx_rates caches published reference rates. One row per publication date, so
-- a rate is fetched once per publication and every quote names the exact rate,
-- source and date it used.

CREATE TABLE IF NOT EXISTS pricing_terms (
    terms_id     TEXT PRIMARY KEY,
    issued_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    valid_until  TIMESTAMPTZ NOT NULL,
    -- Identity of the configuration and rate a quote was built from. A new
    -- 402 reuses a quote only while this matches, so a config or rate change
    -- starts a fresh quote immediately.
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
