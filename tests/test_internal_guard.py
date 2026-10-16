"""The core /internal money surface always requires the shared secret.

These exercise the guard only, which short-circuits before any database
access, so no PostgreSQL is needed.
"""
import unittest
from unittest.mock import patch

import httpx

from core import config
from core.api import server
from core.bot import bot

SECRET = "s" * config.MIN_SECRET_LENGTH


async def _call(path, headers=None):
    transport = httpx.ASGITransport(app=server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://core") as client:
        return await client.post(path, headers=headers or {})


class InternalGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_secret_is_rejected(self):
        with patch.object(server, "CORE_INTERNAL_SECRET", SECRET):
            resp = await _call("/internal/verifies/x/payment")
        self.assertEqual(resp.status_code, 401)

    async def test_wrong_secret_is_rejected(self):
        with patch.object(server, "CORE_INTERNAL_SECRET", SECRET):
            resp = await _call("/internal/verifies/x/payment", {"x-internal-secret": "nope"})
        self.assertEqual(resp.status_code, 401)

    async def test_unconfigured_secret_fails_closed(self):
        # An empty header must not match an empty secret.
        with patch.object(server, "CORE_INTERNAL_SECRET", ""):
            resp = await _call("/internal/verifies/x/payment", {"x-internal-secret": ""})
        self.assertEqual(resp.status_code, 401)

    async def test_non_ascii_secret_is_rejected_not_crashed(self):
        with patch.object(server, "CORE_INTERNAL_SECRET", SECRET):
            resp = await _call("/internal/verifies/x/payment", {"x-internal-secret": "ä".encode()})
        self.assertEqual(resp.status_code, 401)

    async def test_correct_secret_passes_the_guard(self):
        # Past the guard the handler validates the empty body: 422, not 401.
        with patch.object(server, "CORE_INTERNAL_SECRET", SECRET):
            resp = await _call("/internal/verifies/x/payment", {"x-internal-secret": SECRET})
        self.assertEqual(resp.status_code, 422)


class RequiredSecretTests(unittest.TestCase):
    def test_short_or_empty_secret_refuses(self):
        for value in ("", "short"):
            with self.assertRaises(RuntimeError):
                config.require_secret("X", value)

    def test_long_secret_is_accepted(self):
        config.require_secret("X", SECRET)

    def test_core_api_refuses_to_start_without_secret(self):
        async def start():
            async with server.lifespan(server.app):
                pass

        import asyncio

        with patch.object(server, "CORE_INTERNAL_SECRET", ""):
            with self.assertRaisesRegex(RuntimeError, "CORE_INTERNAL_SECRET"):
                asyncio.run(start())

    def test_core_api_refuses_a_short_dashboard_token(self):
        async def start():
            async with server.lifespan(server.app):
                pass

        import asyncio

        with patch.object(server, "CORE_INTERNAL_SECRET", SECRET), \
                patch.object(config, "DASHBOARD_TOKEN", "short"):
            with self.assertRaisesRegex(RuntimeError, "DASHBOARD_TOKEN"):
                asyncio.run(start())

    def test_bot_webhook_mode_refuses_to_start_without_secret(self):
        with patch.object(config, "TELEGRAM_BOT_TOKEN", "123:abc"), \
                patch.object(config, "BOT_MODE", "webhook"), \
                patch.object(config, "TELEGRAM_WEBHOOK_SECRET", ""), \
                patch.object(bot, "build_application") as build:
            with self.assertRaises(SystemExit) as ctx:
                bot.main()
        self.assertIn("TELEGRAM_WEBHOOK_SECRET", str(ctx.exception))
        build.assert_not_called()


if __name__ == "__main__":
    unittest.main()
