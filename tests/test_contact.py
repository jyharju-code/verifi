import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from core.api.server import ContactIn, contact


class ContactDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def payload(self, **changes):
        values = {
            "name": "Production test",
            "email": "test@example.com",
            "message": "Please confirm Telegram delivery.",
        }
        values.update(changes)
        return ContactIn(**values)

    async def test_success_requires_a_telegram_message_id(self):
        audit = AsyncMock()
        send = AsyncMock(return_value=321)
        with (
            patch("core.api.server.audit", audit),
            patch("core.api.server.notify.send_message", send),
            patch("core.api.server.config.TELEGRAM_BOT_TOKEN", "existing-token"),
            patch("core.api.server.config.ADMIN_TELEGRAM_ID", 123),
        ):
            result = await contact(self.payload())

        self.assertEqual(result, {"ok": True, "delivery": "telegram"})
        send.assert_awaited_once()
        self.assertEqual(audit.await_args_list[-1].args[1], "contact_message_delivered")

    async def test_telegram_rejection_is_not_reported_as_success(self):
        with (
            patch("core.api.server.audit", new=AsyncMock()),
            patch("core.api.server.notify.send_message", new=AsyncMock(return_value=None)),
            patch("core.api.server.config.TELEGRAM_BOT_TOKEN", "existing-token"),
            patch("core.api.server.config.ADMIN_TELEGRAM_ID", 123),
        ):
            with self.assertRaises(HTTPException) as raised:
                await contact(self.payload())

        self.assertEqual(raised.exception.status_code, 502)

    async def test_honeypot_is_filtered_without_telegram(self):
        send = AsyncMock()
        with (
            patch("core.api.server.audit", new=AsyncMock()),
            patch("core.api.server.notify.send_message", send),
        ):
            result = await contact(self.payload(company_website="https://spam.example"))

        self.assertEqual(result, {"ok": True, "delivery": "filtered"})
        send.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
