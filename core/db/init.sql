-- Verifi database bootstrap. Run as: psql -d verifi -f core/db/init.sql
-- Order matters: instances before associates before verifies.

-- pgvector is part of the declared stack for later semantic features.
-- Comment this out if the extension is not installed yet; nothing uses it today.
-- CREATE EXTENSION IF NOT EXISTS vector;

\i core/db/instances.sql
\i core/db/associates.sql
\i core/db/verifies.sql
\i core/db/audit.sql
