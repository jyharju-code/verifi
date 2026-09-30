"""Disposable PostgreSQL databases for tests that need the real schema.

Set VERIFI_TEST_DATABASE_URL to an admin connection on a throwaway server, for
example postgresql://verifi@127.0.0.1:54339/postgres. Each test gets a fresh
database loaded in exactly the production order (read from
deploy/initdb/00-schema.sh, so there is one source for it), then every
migration is applied the way the core API applies them at startup.

Without the variable these tests are skipped, so a plain unit run needs no
database. CI provides one.
"""
import os
import re
import unittest
import uuid
from pathlib import Path

import asyncpg

from core import config
from core.db import database, migrate

REPO = Path(__file__).resolve().parents[1]
ADMIN_URL = os.environ.get("VERIFI_TEST_DATABASE_URL", "")

requires_db = unittest.skipUnless(ADMIN_URL, "VERIFI_TEST_DATABASE_URL is not set")


def schema_files() -> list[Path]:
    script = (REPO / "deploy" / "initdb" / "00-schema.sh").read_text()
    names = re.search(r"for f in ([^;]+); do", script).group(1).split()
    return [REPO / "core" / "db" / name for name in names]


def _with_db(url: str, name: str) -> str:
    return re.sub(r"/[^/]*$", f"/{name}", url)


class DatabaseTestCase(unittest.IsolatedAsyncioTestCase):
    """Fresh schema per test. self.db is the pool the application code uses."""

    async def asyncSetUp(self):
        self.db_name = f"verifi_test_{uuid.uuid4().hex[:12]}"
        admin = await asyncpg.connect(ADMIN_URL)
        try:
            await admin.execute(f'CREATE DATABASE "{self.db_name}"')
        finally:
            await admin.close()
        self.dsn = _with_db(ADMIN_URL, self.db_name)
        conn = await asyncpg.connect(self.dsn)
        try:
            for path in schema_files():
                await conn.execute(path.read_text())
        finally:
            await conn.close()
        self._saved_url = config.DATABASE_URL
        config.DATABASE_URL = self.dsn
        await database.close_pool()
        await migrate.migrate()
        self.db = await database.get_pool()

    async def asyncTearDown(self):
        await database.close_pool()
        config.DATABASE_URL = self._saved_url
        admin = await asyncpg.connect(ADMIN_URL)
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{self.db_name}" WITH (FORCE)')
        finally:
            await admin.close()
