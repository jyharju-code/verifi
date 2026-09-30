"""Gate 1 of contract v3: binding the terms at admission.

Runs the real core functions against a real PostgreSQL. No associate exists,
so admission routes nobody and no Telegram call is made.
"""
from datetime import date
from decimal import Decimal
from unittest.mock import patch
from uuid import UUID

from fastapi import HTTPException

from core import fx as fx_rates
from core.api import server
from tests.dbutil import DatabaseTestCase, requires_db

USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
EURC = "0x60a3E35Cc302bFA44Cb288Bc5a4F316Fdb1adb42"
WALLET = "0x2222222222222222222222222222222222222222"


def x402_request(terms_id, asset, wallet=WALLET):
    return server.VerifyIn(
        instance="verify-api", intent="Check a claim", claim="The sky is green.",
        agent_id=wallet, admission_mode="x402", terms_id=terms_id, paid_asset=asset,
    )


class Gate1Base(DatabaseTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        server._PRICING = None
        await fx_rates.store_rate(self.db, date(2026, 9, 30), Decimal("1.17"))
        self.quote = await server.terms_current()

    async def asyncTearDown(self):
        server._PRICING = None
        await super().asyncTearDown()

    async def pay_entry(self, verify_id, tx="0x" + "e1" * 32):
        return await server.record_payment(
            UUID(verify_id), server.PaymentIn(kind="entry", transaction=tx, payer=WALLET)
        )

    async def row(self, verify_id):
        return await self.db.fetchrow("SELECT * FROM verifies WHERE id = $1", UUID(verify_id))


@requires_db
class X402AdmissionTests(Gate1Base):
    async def test_creation_binds_the_paid_asset_but_does_not_admit(self):
        created = await server.create_verify(x402_request(self.quote["terms_id"], EURC))
        row = await self.row(created["id"])
        self.assertEqual(row["contract_version"], 3)
        self.assertEqual(row["terms_id"], self.quote["terms_id"])
        self.assertEqual(row["bound_asset"], EURC)
        self.assertEqual(row["entry_amount_atomic"], 100_000)
        self.assertEqual(row["status"], "admission_pending")
        # Admission follows settlement: no windows before the entry settles.
        self.assertIsNone(row["admitted_at"])
        self.assertIsNone(row["sla_deadline"])

    async def test_settlement_admits_and_starts_the_windows(self):
        created = await server.create_verify(x402_request(self.quote["terms_id"], EURC))
        await self.pay_entry(created["id"])
        row = await self.row(created["id"])
        self.assertEqual(row["status"], "pending")
        self.assertEqual((row["sla_deadline"] - row["admitted_at"]).total_seconds(), 3600)
        self.assertEqual((row["grace_deadline"] - row["admitted_at"]).total_seconds(), 86400)
        self.assertEqual(row["expires_at"], row["grace_deadline"])
        self.assertEqual(row["entry_charged_atomic"], 100_000)
        # An EURC amount never lands in a *_usdc column.
        self.assertEqual(row["entry_charged_usdc"], Decimal("0.00"))

    async def test_a_usdc_admission_keeps_the_usdc_columns_truthful(self):
        created = await server.create_verify(x402_request(self.quote["terms_id"], USDC))
        await self.pay_entry(created["id"])
        row = await self.row(created["id"])
        self.assertEqual(row["entry_amount_atomic"], 120_000)
        self.assertEqual(row["entry_charged_usdc"], Decimal("0.12"))

    async def test_a_slow_settlement_does_not_eat_into_the_sla_window(self):
        created = await server.create_verify(x402_request(self.quote["terms_id"], EURC))
        await self.db.execute(
            "UPDATE verifies SET created_at = now() - interval '9 minutes' WHERE id = $1",
            UUID(created["id"]),
        )
        await self.pay_entry(created["id"])
        row = await self.row(created["id"])
        self.assertEqual((row["sla_deadline"] - row["admitted_at"]).total_seconds(), 3600)
        self.assertGreater((row["sla_deadline"] - row["created_at"]).total_seconds(), 3600 + 8 * 60)

    async def test_the_binding_is_audited_once_even_if_the_settlement_is_replayed(self):
        created = await server.create_verify(x402_request(self.quote["terms_id"], EURC))
        await self.pay_entry(created["id"])
        await self.pay_entry(created["id"])
        events = await self.db.fetch("SELECT details FROM audit_log WHERE event = 'terms_bound'")
        self.assertEqual(len(events), 1)

    async def test_a_quote_that_expired_after_payment_still_binds(self):
        await self.db.execute("UPDATE pricing_terms SET valid_until = now() - interval '1 second'")
        created = await server.create_verify(x402_request(self.quote["terms_id"], EURC))
        self.assertEqual((await self.row(created["id"]))["terms_id"], self.quote["terms_id"])

    async def test_missing_unknown_or_foreign_terms_are_refused(self):
        cases = [
            (None, EURC),
            ("st_nope", EURC),
            (self.quote["terms_id"], "0x" + "9" * 40),
        ]
        for terms_id, asset in cases:
            with self.subTest(terms_id=terms_id, asset=asset), self.assertRaises(HTTPException) as ctx:
                await server.create_verify(x402_request(terms_id, asset))
            self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(await self.db.fetchval("SELECT count(*) FROM verifies"), 0)

    async def test_bound_terms_do_not_move_when_config_or_rate_change(self):
        created = await server.create_verify(x402_request(self.quote["terms_id"], USDC))
        await self.pay_entry(created["id"])
        before = await self.row(created["id"])
        await fx_rates.store_rate(self.db, date(2026, 10, 1), Decimal("1.30"))
        with patch.dict("os.environ", {"SLA_UNLOCK_EUR": "3.50", "SLA_SECONDS": "600"}):
            server._PRICING = None
            fresh = await server.terms_current()
        self.assertNotEqual(fresh["terms_id"], self.quote["terms_id"])
        after = await self.row(created["id"])
        for column in ("bound_terms", "entry_amount_atomic", "sla_deadline", "grace_deadline", "expires_at"):
            self.assertEqual(before[column], after[column], column)


@requires_db
class NoGraceAdmissionTests(Gate1Base):
    async def asyncSetUp(self):
        with patch.dict("os.environ", {"GRACE_SECONDS": "0"}):
            await super().asyncSetUp()

    async def test_without_grace_the_item_expires_at_the_sla_deadline(self):
        created = await server.create_verify(x402_request(self.quote["terms_id"], EURC))
        await self.pay_entry(created["id"])
        row = await self.row(created["id"])
        self.assertIsNone(row["grace_deadline"])
        self.assertEqual(row["expires_at"], row["sla_deadline"])


@requires_db
class CreditAdmissionTests(Gate1Base):
    async def grant_credit(self, source_asset):
        source = await self.db.fetchval(
            """
            INSERT INTO verifies (instance, intent, claim, agent_id, tier, status, entry_source,
                                  contract_version, terms_id, bound_terms, bound_asset)
            VALUES ('verify-api', 'i', 'c', $1, 'paid', 'expired', 'x402',
                    $2, $3, $4::jsonb, $5)
            RETURNING id
            """,
            WALLET,
            3 if source_asset else 2,
            self.quote["terms_id"] if source_asset else None,
            '{"asset_symbol": "x"}' if source_asset else None,
            source_asset,
        )
        await self.db.execute(
            "INSERT INTO wallet_entitlements (instance, wallet_address, kind, source_verify_id) "
            "VALUES ('verify-api', $1, 'failure_credit', $2)",
            WALLET,
            source,
        )

    def credit_request(self):
        return server.VerifyIn(
            instance="verify-api", intent="i", claim="c", agent_id=WALLET, admission_mode="entitlement",
        )

    async def test_a_credit_binds_to_the_asset_that_earned_it(self):
        await self.grant_credit(EURC)
        created = await server.create_verify(self.credit_request())
        row = await self.row(created["id"])
        self.assertEqual((row["contract_version"], row["bound_asset"]), (3, EURC))
        self.assertEqual(row["entry_source"], "failure_credit")
        self.assertEqual(row["entry_charged_atomic"], 0)
        # A credit admission is admitted at once, so its windows start now.
        self.assertEqual(row["status"], "pending")
        self.assertEqual((row["sla_deadline"] - row["admitted_at"]).total_seconds(), 3600)

    async def test_a_credit_from_before_v3_binds_to_usdc(self):
        await self.grant_credit(None)
        created = await server.create_verify(self.credit_request())
        self.assertEqual((await self.row(created["id"]))["bound_asset"], USDC)

    async def test_without_pricing_a_credit_is_refused_and_kept(self):
        await self.grant_credit(EURC)
        await self.db.execute("DELETE FROM verifies WHERE status = 'admission_pending'")
        await self.db.execute("UPDATE pricing_terms SET valid_until = now() - interval '1 hour'")
        await self.db.execute("DELETE FROM fx_rates")
        with self.assertRaises(HTTPException) as ctx:
            await server.create_verify(self.credit_request())
        self.assertEqual(ctx.exception.status_code, 503)
        unused = await self.db.fetchval(
            "SELECT count(*) FROM wallet_entitlements WHERE consumed_by_verify_id IS NULL"
        )
        self.assertEqual(unused, 1)

    async def test_the_credit_binding_is_audited(self):
        await self.grant_credit(EURC)
        await server.create_verify(self.credit_request())
        details = await self.db.fetchval("SELECT details FROM audit_log WHERE event = 'terms_bound'")
        self.assertIn('"entry_source": "failure_credit"', details)
