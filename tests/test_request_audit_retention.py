import unittest
from unittest.mock import AsyncMock, patch

from core.api import server


class RequestAuditRetentionTests(unittest.IsolatedAsyncioTestCase):
    def test_retention_is_never_more_than_30_days(self):
        self.assertGreaterEqual(server.REQUEST_AUDIT_RETENTION_DAYS, 1)
        self.assertLessEqual(server.REQUEST_AUDIT_RETENTION_DAYS, 30)

    async def test_prune_uses_the_capped_retention(self):
        db = AsyncMock()
        db.execute.return_value = "DELETE 2"
        with patch("core.api.server.get_pool", new=AsyncMock(return_value=db)):
            result = await server._prune_request_audit_once()

        self.assertEqual(result, "DELETE 2")
        sql, days = db.execute.await_args.args
        self.assertIn("DELETE FROM request_audit", sql)
        self.assertEqual(days, server.REQUEST_AUDIT_RETENTION_DAYS)


if __name__ == "__main__":
    unittest.main()
