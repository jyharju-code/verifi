-- Product instances (verify-api, atf, ...).
-- One row per product built on the Verifi core engine.

CREATE TABLE IF NOT EXISTS instances (
    id                   VARCHAR(50) PRIMARY KEY,
    name                 VARCHAR(200) NOT NULL,
    price_per_verify     NUMERIC(10,2) NOT NULL DEFAULT 1.00,
    associate_commission NUMERIC(10,2) NOT NULL DEFAULT 0.50,
    free_tier_count      INTEGER NOT NULL DEFAULT 0,
    status               VARCHAR(20) NOT NULL DEFAULT 'active'
                         CHECK (status IN ('active', 'paused', 'disabled')),
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO instances (id, name, price_per_verify, associate_commission, free_tier_count, status)
VALUES
    ('verify-api', 'Verify API', 3.00, 0.50, 5, 'active'),
    ('atf', 'Ask This Finn', 2.22, 1.00, 0, 'paused')
ON CONFLICT (id) DO NOTHING;
