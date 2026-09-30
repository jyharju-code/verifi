"""Operator dashboard under contract v3: money per asset, prices read-only."""
from unittest.mock import patch

import httpx

from core import config
from core.api import server
from core.bot.handlers import verify_buttons
from tests.dbutil import requires_db
from tests.test_gate1_binding import EURC, USDC
from tests.test_windows import WindowBase

TOKEN = "dashboard-test-token"


@requires_db
class DashboardTests(WindowBase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.token = patch.object(config, "DASHBOARD_TOKEN", TOKEN)
        self.token.start()
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=server.app), base_url="http://test",
            headers={"cookie": f"verifi_dash={TOKEN}"},
        )

    async def asyncTearDown(self):
        await self.client.aclose()
        self.token.stop()
        await super().asyncTearDown()

    async def test_revenue_and_owed_are_per_asset(self):
        assoc = await self.db.fetchval(
            "INSERT INTO associates (name, username, telegram_id, status) VALUES ('A', 'a', 7, 'active') RETURNING id"
        )
        vid = await self.admitted(EURC)
        await self.db.execute("UPDATE verifies SET associate_id = $2 WHERE id = $1", vid, assoc)
        verify = await self.row(str(vid))
        async with self.db.acquire() as conn:
            async with conn.transaction():
                await verify_buttons._resolve(conn, verify, "accepted", "accepted")
        await self.db.execute(
            "INSERT INTO verifies (instance, intent, claim, agent_id, tier, status, entry_source, entry_charged_usdc) "
            "VALUES ('verify-api', 'i', 'c', '0x3333333333333333333333333333333333333333', 'paid', 'accepted', 'x402', 0.10)"
        )
        data = (await self.client.get("/admin/data")).json()
        revenue = {r["asset"]: r["total"] for r in data["totals"]["revenue"]}
        self.assertEqual(revenue, {"EURC": 0.1, "USDC": 0.1})
        self.assertEqual(data["totals"]["owed"], [{"asset": "EURC", "amount": 0.5}])
        [row] = [a for a in data["associates"] if a["username"] == "a"]
        self.assertEqual(row["balances"], [{"asset": "EURC", "earned": 0.5, "pending": 0.5}])
        charged = {r["charged"]["asset"]: r["charged"]["entry"] for r in data["recent"]}
        self.assertEqual(charged, {"EURC": 0.1, "USDC": 0.1})
        self.assertEqual(data["terms"]["terms_id"], self.quote["terms_id"])
        self.assertNotIn("price", data["instances"][0])

    async def test_only_the_commission_is_editable_and_bounded_by_the_sla_price(self):
        ok = await self.client.post("/admin/instances/verify-api/pricing", json={"commission": 0.40})
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.json()["commission"], 0.4)
        too_high = await self.client.post("/admin/instances/verify-api/pricing", json={"commission": 3.00})
        self.assertEqual(too_high.status_code, 422)
        self.assertEqual(
            str(await self.db.fetchval("SELECT associate_commission FROM instances WHERE id = 'verify-api'")),
            "0.40",
        )

    async def test_a_usdc_chain_reports_its_converted_charge(self):
        vid = await self.admitted(USDC)
        data = (await self.client.get("/admin/data")).json()
        [row] = [r for r in data["recent"] if r["verify_id"] == str(vid)]
        self.assertEqual(row["charged"], {"asset": "USDC", "entry": 0.12, "unlock": 0.0, "total": 0.12})
