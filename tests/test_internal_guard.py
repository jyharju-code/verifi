"""The core /internal money surface requires the shared secret when configured.

These exercise the rejection path only, which short-circuits before any
database access, so no PostgreSQL is needed.
"""
import unittest
from unittest.mock import patch

import httpx

from core.api import server


async def _call(path, headers=None):
    transport = httpx.ASGITransport(app=server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://core") as client:
        return await client.post(path, headers=headers or {})


class InternalGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_secret_is_rejected_when_configured(self):
        with patch.object(server, "CORE_INTERNAL_SECRET", "s3cret"):
            resp = await _call("/internal/verifies/x/payment")
        self.assertEqual(resp.status_code, 401)

    async def test_wrong_secret_is_rejected_when_configured(self):
        with patch.object(server, "CORE_INTERNAL_SECRET", "s3cret"):
            resp = await _call("/internal/verifies/x/payment", {"x-internal-secret": "nope"})
        self.assertEqual(resp.status_code, 401)


if __name__ == "__main__":
    unittest.main()
