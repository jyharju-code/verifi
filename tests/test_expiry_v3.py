"""Expiry of v3 chains: the last window, not a fixed hour, and the credit's asset."""
import json
from unittest.mock import AsyncMock, patch
from uuid import UUID

from core.api import server
from tests.dbutil import requires_db
from tests.test_gate1_binding import EURC, USDC, WALLET
from tests.test_windows import WindowBase


@requires_db
class V3ExpiryTests(WindowBase):
    async def expire(self):
        with patch.object(server.notify, "send_message", AsyncMock()):
            return await server._expire_stale_once()

    async def test_a_chain_past_its_sla_but_inside_grace_keeps_waiting(self):
        vid = await self.admitted(EURC)
        await self.age(vid, "3 hours")
        self.assertEqual(await self.expire(), 0)
        self.assertEqual((await self.row(str(vid)))["status"], "pending")

    async def test_a_chain_past_its_last_window_expires_with_a_credit_in_its_asset(self):
        vid = await self.admitted(EURC)
        await self.age(vid, "24 hours 1 second")
        self.assertEqual(await self.expire(), 1)
        row = await self.row(str(vid))
        self.assertEqual((row["status"], row["failure_reason"]), ("expired", "human_timeout"))
        self.assertTrue(row["failure_credit_granted"])
        details = await self.db.fetchval(
            "SELECT details FROM wallet_entitlements WHERE source_verify_id = $1", vid
        )
        self.assertEqual(
            json.loads(details),
            {"reason": "human_timeout", "asset": EURC, "entry_amount_atomic": "100000"},
        )
        audit = json.loads(await self.db.fetchval(
            "SELECT details FROM audit_log WHERE event = 'verify_failed_credit_granted'"
        ))
        self.assertEqual((audit["credit_asset"], audit["credit_amount_atomic"]), (EURC, "100000"))
        self.assertIsNone(audit["credit_usdc"])

    async def test_the_credit_admits_the_next_chain_in_the_same_asset(self):
        vid = await self.admitted(EURC)
        await self.age(vid, "25 hours")
        await self.expire()
        created = await server.create_verify(server.VerifyIn(
            instance="verify-api", intent="i", claim="c", agent_id=WALLET, admission_mode="entitlement",
        ))
        row = await self.row(created["id"])
        self.assertEqual((row["entry_source"], row["bound_asset"]), ("failure_credit", EURC))

    async def test_an_expiring_credit_chain_returns_its_credit_instead_of_minting_one(self):
        vid = await self.admitted(USDC)
        await self.age(vid, "25 hours")
        await self.expire()
        created = await server.create_verify(server.VerifyIn(
            instance="verify-api", intent="i", claim="c", agent_id=WALLET, admission_mode="entitlement",
        ))
        await self.age(UUID(created["id"]), "25 hours")
        await self.expire()
        credits = await self.db.fetch(
            "SELECT consumed_by_verify_id FROM wallet_entitlements WHERE kind = 'failure_credit'"
        )
        self.assertEqual(len(credits), 1)
        self.assertIsNone(credits[0]["consumed_by_verify_id"])

    async def test_without_grace_the_chain_expires_after_the_sla(self):
        with patch.dict("os.environ", {"GRACE_SECONDS": "0"}):
            server._PRICING = None
            self.quote = await server.terms_current()
        vid = await self.admitted(EURC)
        await self.age(vid, "61 minutes")
        self.assertEqual(await self.expire(), 1)
