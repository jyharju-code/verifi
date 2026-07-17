#!/bin/bash
# Runs once when the postgres volume is empty. Loads the Verifi schema
# in dependency order from /schema (mounted read-only from core/db).
set -e
psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" <<'SQL'
CREATE EXTENSION IF NOT EXISTS vector;
SQL
for f in instances.sql associates.sql verifies.sql audit.sql; do
    psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -f "/schema/$f"
done
