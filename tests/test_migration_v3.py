"""The contract v3 migration must leave every v2 chain exactly as it was."""
from pathlib import Path

from tests.dbutil import REPO, DatabaseTestCase, requires_db

MIGRATION = REPO / "core" / "db" / "migrations" / "2026-10-01-contract-v3-three-gate.sql"
WALLET = "0x1111111111111111111111111111111111111111"


@requires_db
class ContractV3MigrationTests(DatabaseTestCase):
    async def _seed_v2_world(self):
        await self.db.execute(
            "INSERT INTO associates (name, username, telegram_id, status, earnings, paid_total) "
            "VALUES ('A', 'a', 1, 'active', 3.50, 1.00)"
        )
        await self.db.execute("INSERT INTO payouts (associate_id, amount, method) VALUES (1, 1.00, 'crypto')")
        return await self.db.fetchval(
            "INSERT INTO verifies (instance, intent, claim, agent_id, tier, status, entry_source, "
            "x402_payment_tx, entry_charged_usdc) "
            "VALUES ('verify-api', 'i', 'c', $1, 'paid', 'accepted', 'x402', '0xabc', 0.10) RETURNING id",
            WALLET,
        )

    async def _rerun(self):
        await self.db.execute(Path(MIGRATION).read_text())

    async def test_v2_rows_keep_v2_semantics(self):
        vid = await self._seed_v2_world()
        await self._rerun()
        row = await self.db.fetchrow("SELECT * FROM verifies WHERE id = $1", vid)
        self.assertEqual(row["contract_version"], 2)
        self.assertIsNone(row["terms_id"])
        self.assertIsNone(row["applied_window"])
        self.assertEqual(str(row["entry_charged_usdc"]), "0.10")

    async def test_legacy_balance_is_carried_once_as_usdc(self):
        await self._seed_v2_world()
        await self._rerun()
        await self._rerun()
        rows = await self.db.fetch("SELECT * FROM associate_earnings")
        opening = [r for r in rows if r["kind"] == "legacy_opening_balance"]
        # The fresh schema starts empty, so the seeded balance arrives on re-run.
        self.assertEqual(len(opening), 1)
        self.assertEqual(opening[0]["asset"], "USDC")
        self.assertEqual(opening[0]["amount_atomic"], 3_500_000)
        payout = await self.db.fetchrow("SELECT asset FROM payouts")
        self.assertEqual(payout["asset"], "USDC")

    async def test_v3_row_without_bound_terms_is_rejected(self):
        with self.assertRaises(Exception):
            await self.db.execute(
                "INSERT INTO verifies (instance, intent, claim, agent_id, tier, contract_version) "
                "VALUES ('verify-api', 'i', 'c', $1, 'paid', 3)",
                WALLET,
            )

    async def test_applied_window_vocabulary_is_enforced(self):
        vid = await self._seed_v2_world()
        with self.assertRaises(Exception):
            await self.db.execute("UPDATE verifies SET applied_window = 'late' WHERE id = $1", vid)
