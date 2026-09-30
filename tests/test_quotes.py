"""Stored quotes: reuse, invalidation, expiry, and the internal endpoints."""
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from fastapi import HTTPException

from core import fx as fx_rates
from core import pricing
from core.api import server
from tests.dbutil import DatabaseTestCase, requires_db

USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"


@requires_db
class QuoteTests(DatabaseTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        server._PRICING = None
        await fx_rates.store_rate(self.db, date(2026, 9, 30), Decimal("1.17"))

    async def asyncTearDown(self):
        server._PRICING = None
        await super().asyncTearDown()

    async def test_a_fresh_quote_is_reused(self):
        first = await server.terms_current()
        second = await server.terms_current()
        self.assertEqual(first["terms_id"], second["terms_id"])
        self.assertEqual(await self.db.fetchval("SELECT count(*) FROM pricing_terms"), 1)

    async def test_the_first_quote_is_audited_once(self):
        await server.terms_current()
        await server.terms_current()
        self.assertEqual(
            await self.db.fetchval("SELECT count(*) FROM audit_log WHERE event = 'terms_quoted'"), 1
        )

    async def test_a_new_rate_starts_a_new_quote(self):
        first = await server.terms_current()
        await fx_rates.store_rate(self.db, date(2026, 10, 1), Decimal("1.18"))
        second = await server.terms_current()
        self.assertNotEqual(first["terms_id"], second["terms_id"])
        usdc = next(p for p in second["terms"]["prices"] if p["asset"] == USDC)
        self.assertEqual(usdc["conversion"]["as_of"], "2026-10-01")

    async def test_a_quote_past_half_its_validity_is_not_handed_out_again(self):
        first = await server.terms_current()
        await self.db.execute(
            "UPDATE pricing_terms SET valid_until = now() + interval '100 seconds'"
        )
        second = await server.terms_current()
        self.assertNotEqual(first["terms_id"], second["terms_id"])

    async def test_a_quote_can_be_looked_up_and_expires(self):
        quote = await server.terms_current()
        found = await server.terms_by_id(quote["terms_id"])
        self.assertTrue(found["valid"])
        self.assertEqual(found["terms"], quote["terms"])
        await self.db.execute("UPDATE pricing_terms SET valid_until = now() - interval '1 second'")
        self.assertFalse((await server.terms_by_id(quote["terms_id"]))["valid"])

    async def test_an_unknown_quote_is_404(self):
        with self.assertRaises(HTTPException) as ctx:
            await server.terms_by_id("st_does_not_exist")
        self.assertEqual(ctx.exception.status_code, 404)

    async def test_without_a_rate_the_quote_is_503_not_partial(self):
        await self.db.execute("DELETE FROM fx_rates")
        with self.assertRaises(HTTPException) as ctx:
            await server.terms_current()
        self.assertEqual(ctx.exception.status_code, 503)

    async def test_an_eurc_only_deployment_quotes_without_a_rate(self):
        await self.db.execute("DELETE FROM fx_rates")
        with patch.dict("os.environ", {"X402_ASSETS": "EURC"}):
            server._PRICING = None
            quote = await server.terms_current()
        self.assertEqual(len(quote["terms"]["prices"]), 1)

    async def test_the_quote_snapshots_the_responder_commission(self):
        await self.db.execute("UPDATE instances SET associate_commission = 0.40 WHERE id = 'verify-api'")
        quote = await server.terms_current()
        self.assertEqual(quote["internal"]["responder_commission_eur"], "0.40")
        self.assertNotIn("responder_commission_eur", quote["terms"])

    async def test_a_rate_is_stored_once_per_publication(self):
        again = await fx_rates.store_rate(self.db, date(2026, 9, 30), Decimal("1.17"))
        self.assertFalse(again)
        latest = await fx_rates.latest_rate(self.db)
        self.assertEqual((latest.rate, latest.as_of), (Decimal("1.17"), date(2026, 9, 30)))
        self.assertIsInstance(latest, pricing.FxRate)
