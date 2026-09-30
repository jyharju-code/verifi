"""Responder earnings: per asset, never converted, following the price paid (Q2)."""
import json
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import patch

from core import notify
from core.bot import payments as bot_payments
from core.bot.handlers import verify_buttons
from core.payments import settlement
from core.payments.settlement import v3_earning
from tests.dbutil import requires_db
from tests.test_gate1_binding import EURC, USDC
from tests.test_windows import WindowBase


class EarningRuleTests(unittest.TestCase):
    def test_an_sla_answer_earns_the_commission_in_the_bound_asset(self):
        self.assertEqual(v3_earning(2_900_000, Decimal("0.50"), Decimal("2.90")), 500_000)
        # USDC at 1.17: 3.40 USDC unlock, so the commission is worth 0.586206 USDC.
        self.assertEqual(v3_earning(3_400_000, Decimal("0.50"), Decimal("2.90")), 586_206)

    def test_a_grace_answer_earns_from_the_grace_price(self):
        self.assertEqual(v3_earning(1_450_000, Decimal("0.50"), Decimal("2.90")), 250_000)
        self.assertEqual(v3_earning(1_700_000, Decimal("0.50"), Decimal("2.90")), 293_103)

    def test_rounding_is_always_down(self):
        self.assertEqual(v3_earning(1, Decimal("0.50"), Decimal("2.90")), 0)


class CardTests(unittest.TestCase):
    base = {"verify_no": 5, "instance": "verify-api", "intent": "i", "claim": "c", "agent_id": "0x" + "1" * 40}

    def test_a_v3_card_states_both_deadlines_in_utc(self):
        card = notify.format_verify_card({
            **self.base,
            "sla_deadline": datetime(2026, 10, 1, 10, 0, 2, tzinfo=timezone.utc),
            "grace_deadline": datetime(2026, 10, 2, 9, 0, 2, tzinfo=timezone.utc),
        })
        self.assertIn("SLA window until 10:00 UTC 01.10.", card)
        self.assertIn("Grace window until 09:00 UTC 02.10.", card)

    def test_a_v2_card_is_unchanged(self):
        card = notify.format_verify_card(self.base)
        self.assertNotIn("window", card)
        self.assertTrue(card.endswith("Requester: agent 0x1111...1111"))


@requires_db
class LedgerTests(WindowBase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.assoc = await self.db.fetchval(
            "INSERT INTO associates (name, username, telegram_id, status, payout_method, wallet_address) "
            "VALUES ('A', 'anna', 42, 'active', 'crypto', '0x4444444444444444444444444444444444444444') "
            "RETURNING id"
        )

    async def answered(self, asset, age=None):
        vid = await self.admitted(asset)
        await self.db.execute("UPDATE verifies SET associate_id = $2 WHERE id = $1", vid, self.assoc)
        if age:
            await self.age(vid, age)
        verify = await self.row(str(vid))
        async with self.db.acquire() as conn:
            async with conn.transaction():
                await verify_buttons._resolve(conn, verify, "accepted", "accepted")
        return vid

    async def ledger(self):
        return await self.db.fetch("SELECT * FROM associate_earnings WHERE kind = 'answer' ORDER BY id")

    async def test_an_eurc_answer_earns_eurc(self):
        await self.answered(EURC)
        [row] = await self.ledger()
        self.assertEqual((row["asset"], row["amount_atomic"], row["applied_window"]), ("EURC", 500_000, "sla"))
        audit = json.loads(await self.db.fetchval("SELECT details FROM audit_log WHERE event = 'earnings_recorded'"))
        self.assertEqual((audit["asset"], audit["amount_atomic"]), ("EURC", "500000"))
        self.assertEqual(await self.db.fetchval("SELECT total_paid FROM associates WHERE id = $1", self.assoc), 1)

    async def test_a_usdc_grace_answer_earns_less_usdc(self):
        await self.answered(USDC, age="2 hours")
        [row] = await self.ledger()
        self.assertEqual((row["asset"], row["amount_atomic"], row["applied_window"]), ("USDC", 293_103, "grace"))

    async def test_the_commission_is_the_one_snapshotted_at_admission(self):
        vid = await self.admitted(EURC)
        await self.db.execute("UPDATE instances SET associate_commission = 1.00 WHERE id = 'verify-api'")
        await self.db.execute("UPDATE verifies SET associate_id = $2 WHERE id = $1", vid, self.assoc)
        verify = await self.row(str(vid))
        async with self.db.acquire() as conn:
            async with conn.transaction():
                await verify_buttons._resolve(conn, verify, "accepted", "accepted")
        [row] = await self.ledger()
        self.assertEqual(row["amount_atomic"], 500_000)

    async def test_a_second_credit_for_the_same_verify_records_nothing(self):
        vid = await self.answered(EURC)
        async with self.db.acquire() as conn:
            self.assertIsNone(await settlement.credit_for_verify(conn, vid))
        self.assertEqual(len(await self.ledger()), 1)

    async def test_a_v2_answer_earns_the_usdc_commission(self):
        vid = await self.db.fetchval(
            """
            INSERT INTO verifies (instance, intent, claim, agent_id, tier, status, entry_source,
                                  admitted_at, expires_at, associate_id)
            VALUES ('verify-api', 'i', 'c', '0x3333333333333333333333333333333333333333', 'paid',
                    'pending', 'x402', now(), now() + interval '30 minutes', $1)
            RETURNING id
            """,
            self.assoc,
        )
        commission = await self.db.fetchval("SELECT associate_commission FROM instances WHERE id = 'verify-api'")
        verify = await self.row(str(vid))
        async with self.db.acquire() as conn:
            async with conn.transaction():
                await verify_buttons._resolve(conn, verify, "accepted", "accepted")
        [row] = await self.ledger()
        self.assertEqual((row["asset"], row["amount_atomic"]), ("USDC", int(commission * 1_000_000)))

    async def test_balances_are_per_asset_and_include_the_legacy_opening(self):
        await self.db.execute(
            "INSERT INTO associate_earnings (associate_id, kind, asset, amount_atomic, decimals, rule) "
            "VALUES ($1, 'legacy_opening_balance', 'USDC', 3000000, 6, 'test')",
            self.assoc,
        )
        await self.db.execute("INSERT INTO payouts (associate_id, amount, method) VALUES ($1, 1.00, 'crypto')", self.assoc)
        await self.answered(EURC)
        async with self.db.acquire() as conn:
            got = {b.asset: (b.earned, b.paid, b.pending) for b in await settlement.balances(conn, self.assoc)}
        self.assertEqual(got["USDC"], (Decimal("3"), Decimal("1.00"), Decimal("2.00")))
        self.assertEqual(got["EURC"], (Decimal("0.5"), Decimal("0"), Decimal("0.5")))

    async def test_a_payout_settles_only_its_own_asset(self):
        await self.answered(EURC)
        with patch("core.bot.payments.audit"):
            reply = await bot_payments.mark_paid("@anna", "0.50", "EURC")
            self.assertIn("0.50 EURC paid", reply)
            refused = await bot_payments.mark_paid("@anna", "0.50", "USDC")
        self.assertIn("only owed 0.00 USDC", refused)
        self.assertEqual(await self.db.fetchval("SELECT asset FROM payouts"), "EURC")
        self.assertIn("Unknown asset", await bot_payments.mark_paid("@anna", "1", "DAI"))

    async def test_the_payout_report_prints_awal_for_usdc_only(self):
        await self.answered(USDC)
        await self.db.execute("DELETE FROM verifies WHERE status <> 'accepted'")
        await self.answered(EURC)
        report = await bot_payments.pending_report()
        self.assertIn("npx awal send $0.58 0x4444444444444444444444444444444444444444", report)
        self.assertIn("Send 0.50 EURC on Base to 0x4444444444444444444444444444444444444444 by hand", report)
