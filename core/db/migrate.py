"""Apply versioned PostgreSQL migrations before the core API starts.

Migration files are committed with the code and tracked in schema_migrations.
Every file must be idempotent because older production databases predate the
ledger and may have received a migration manually.
"""
import asyncio
import logging
from pathlib import Path

import asyncpg

from core import config

log = logging.getLogger("verifi.migrate")
MIGRATIONS = Path(__file__).with_name("migrations")
LOCK_NAME = "verifi-schema-migrations"


async def migrate() -> list[str]:
    conn = await asyncpg.connect(config.DATABASE_URL)
    applied: list[str] = []
    try:
        await conn.execute("SELECT pg_advisory_lock(hashtext($1))", LOCK_NAME)
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                name        TEXT PRIMARY KEY,
                applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        for path in sorted(MIGRATIONS.glob("*.sql")):
            exists = await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)",
                path.name,
            )
            if exists:
                continue
            sql = path.read_text(encoding="utf-8")
            async with conn.transaction():
                await conn.execute(sql)
                await conn.execute(
                    "INSERT INTO schema_migrations (name) VALUES ($1)",
                    path.name,
                )
            applied.append(path.name)
            log.info("applied migration %s", path.name)
    finally:
        try:
            await conn.execute("SELECT pg_advisory_unlock(hashtext($1))", LOCK_NAME)
        finally:
            await conn.close()
    return applied


async def _main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    applied = await migrate()
    if applied:
        log.info("database migration complete: %s", ", ".join(applied))
    else:
        log.info("database already current")


if __name__ == "__main__":
    asyncio.run(_main())
