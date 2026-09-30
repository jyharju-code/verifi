"""When the human answers: the applied window and the unlock price (D6).

Boundaries are tested exactly: inside one transaction now() is fixed, so a
deadline set to now() in that transaction is the boundary instant itself.
"""
from datetime import date
from decimal import Decimal
from uuid import UUID

from core import fx as fx_rates
from core import windows
from core.api import server
from core.bot.handlers import verify_buttons
from tests.dbutil import requires_db
from tests.test_gate1_binding import EURC, USDC, Gate1Base, x402_request


class WindowBase(Gate1Base):
    async def admitted(self, asset=USDC):
        created = await server.create_verify(x402_request(self.quote["terms_id"], asset))
        await self.pay_entry(created["id"])
        return UUID(created["id"])

    async def age(self, verify_id, interval: str):
        """Move the whole chain into the past, as if admitted `interval` ago."""
        await self.db.execute(
            f"""
            UPDATE verifies
            SET admitted_at = admitted_at - interval '{interval}',
                sla_deadline = sla_deadline - interval '{interval}',
                grace_deadline = grace_deadline - interval '{interval}',
                expires_at = expires_at - interval '{interval}'
            WHERE id = $1
            """,
            verify_id,
        )

    async def answer(self, verify_id, set_clause=None):
        async with self.db.acquire() as conn:
            async with conn.transaction():
                if set_clause:
                    await conn.execute(f"UPDATE verifies SET {set_clause} WHERE id = $1", verify_id)
                return await windows.record_answer(conn, verify_id, "accepted", "accepted")


@requires_db
class AppliedWindowTests(WindowBase):
    async def test_an_answer_inside_the_sla_window_takes_the_sla_price(self):
        vid = await self.admitted()
        row = await self.answer(vid)
        self.assertEqual((row["applied_window"], row["unlock_amount_atomic"]), ("sla", 3_400_000))

    async def test_the_sla_boundary_instant_belongs_to_the_sla_window(self):
        vid = await self.admitted()
        row = await self.answer(vid, "sla_deadline = now()")
        self.assertEqual(row["applied_window"], "sla")

    async def test_just_after_the_sla_deadline_the_grace_price_applies(self):
        vid = await self.admitted()
        row = await self.answer(vid, "sla_deadline = now() - interval '1 microsecond'")
        self.assertEqual((row["applied_window"], row["unlock_amount_atomic"]), ("grace", 1_700_000))

    async def test_an_answer_hours_later_takes_the_grace_price(self):
        vid = await self.admitted(EURC)
        await self.age(vid, "5 hours")
        row = await self.answer(vid)
        self.assertEqual((row["applied_window"], row["unlock_amount_atomic"]), ("grace", 1_450_000))

    async def test_the_grace_boundary_instant_belongs_to_the_grace_window(self):
        vid = await self.admitted()
        await self.age(vid, "2 hours")
        row = await self.answer(vid, "grace_deadline = now(), expires_at = now()")
        self.assertEqual(row["applied_window"], "grace")

    async def test_after_the_last_window_the_answer_is_refused_and_nothing_changes(self):
        vid = await self.admitted()
        await self.age(vid, "25 hours")
        with self.assertRaises(windows.AnswerRefused) as ctx:
            await self.answer(vid)
        self.assertEqual(ctx.exception.reason, "expired")
        row = await self.row(str(vid))
        self.assertEqual(row["status"], "pending")
        self.assertIsNone(row["ready_at"])
        self.assertIsNone(row["unlock_amount_atomic"])

    async def test_ready_at_is_the_answer_time_and_the_record_is_consistent(self):
        vid = await self.admitted()
        await self.answer(vid)
        row = await self.row(str(vid))
        self.assertEqual(row["ready_at"], row["responded_at"])
        self.assertLessEqual(row["ready_at"], row["sla_deadline"])

    async def test_the_price_ignores_rate_and_config_changes_after_admission(self):
        vid = await self.admitted()
        await fx_rates.store_rate(self.db, date(2026, 10, 1), Decimal("1.40"))
        server._PRICING = None
        await server.terms_current()
        row = await self.answer(vid)
        self.assertEqual(row["unlock_amount_atomic"], 3_400_000)

    async def test_a_second_answer_is_refused_as_already_resolved(self):
        vid = await self.admitted()
        await self.answer(vid)
        with self.assertRaises(windows.AnswerRefused) as ctx:
            await self.answer(vid)
        self.assertEqual(ctx.exception.reason, "already_resolved")


@requires_db
class NoGraceWindowTests(WindowBase):
    async def asyncSetUp(self):
        from unittest.mock import patch

        with patch.dict("os.environ", {"GRACE_SECONDS": "0"}):
            await super().asyncSetUp()

    async def test_without_grace_an_answer_after_the_sla_is_refused(self):
        vid = await self.admitted()
        await self.age(vid, "61 minutes")
        with self.assertRaises(windows.AnswerRefused) as ctx:
            await self.answer(vid)
        self.assertEqual(ctx.exception.reason, "expired")


@requires_db
class V2AnswerTests(WindowBase):
    async def v2_chain(self, expires_in: str):
        return await self.db.fetchval(
            f"""
            INSERT INTO verifies (instance, intent, claim, agent_id, tier, status, entry_source,
                                  admitted_at, expires_at)
            VALUES ('verify-api', 'i', 'c', '0x3333333333333333333333333333333333333333', 'paid',
                    'pending', 'x402', now(), now() + interval '{expires_in}')
            RETURNING id
            """
        )

    async def test_a_v2_chain_gets_ready_at_but_no_window_or_amount(self):
        vid = await self.v2_chain("30 minutes")
        row = await self.answer(vid)
        self.assertIsNotNone(row["ready_at"])
        self.assertIsNone(row["applied_window"])
        self.assertIsNone(row["unlock_amount_atomic"])

    async def test_a_v2_chain_past_its_hour_is_refused(self):
        vid = await self.v2_chain("-1 second")
        with self.assertRaises(windows.AnswerRefused) as ctx:
            await self.answer(vid)
        self.assertEqual(ctx.exception.reason, "expired")


@requires_db
class BotResolveTests(WindowBase):
    async def test_the_bot_records_the_window_and_audits_it(self):
        vid = await self.admitted(EURC)
        verify = await self.row(str(vid))
        async with self.db.acquire() as conn:
            async with conn.transaction():
                row = await verify_buttons._resolve(conn, verify, "accepted", "accepted")
        self.assertEqual(row["applied_window"], "sla")
        self.assertEqual(verify_buttons._window_line(row), "Answered within the SLA window.\n")
        details = await self.db.fetchval("SELECT details FROM audit_log WHERE event = 'window_applied'")
        self.assertIn('"applied_window": "sla"', details)
        self.assertIn('"unlock_amount_atomic": "2900000"', details)

    async def test_the_refusal_message_names_expiry(self):
        self.assertIn("expired", verify_buttons._refusal(windows.AnswerRefused("expired")))
        self.assertIn("already handled", verify_buttons._refusal(windows.AnswerRefused("already_resolved")))
