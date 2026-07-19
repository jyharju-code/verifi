"""Wallet safety.

core-api watches the money addresses but must never hold or expose a key.
"""
import unittest
import httpx
from unittest.mock import AsyncMock, patch

from core import wallets


class PrivateKeyIsolationTests(unittest.TestCase):
    def test_startup_refuses_a_leaked_private_key(self):
        for name in ("FACILITATOR_PRIVATE_KEY", "EVM_PRIVATE_KEY", "PRIVATE_KEY"):
            with self.subTest(name=name):
                with patch.dict("os.environ", {name: "0x" + "a" * 64}, clear=False):
                    with self.assertRaises(wallets.WalletConfigError):
                        wallets.assert_no_private_keys()

    def test_startup_passes_without_private_keys(self):
        env = {k: "" for k in ("FACILITATOR_PRIVATE_KEY", "EVM_PRIVATE_KEY", "PRIVATE_KEY")}
        with patch.dict("os.environ", env, clear=False):
            wallets.assert_no_private_keys()


class AddressValidationTests(unittest.TestCase):
    def test_missing_addresses_are_reported(self):
        with patch.object(wallets, "FACILITATOR_ADDRESS", ""), \
             patch.object(wallets, "X402_PAY_TO", ""):
            problems = wallets.validate_addresses()
        self.assertEqual(len(problems), 2)

    def test_malformed_address_is_reported(self):
        with patch.object(wallets, "FACILITATOR_ADDRESS", "not-an-address"), \
             patch.object(wallets, "X402_PAY_TO", "0x" + "1" * 40):
            problems = wallets.validate_addresses()
        self.assertEqual(len(problems), 1)
        self.assertIn("FACILITATOR_ADDRESS", problems[0])

    def test_valid_addresses_report_nothing(self):
        with patch.object(wallets, "FACILITATOR_ADDRESS", "0x" + "1" * 40), \
             patch.object(wallets, "X402_PAY_TO", "0x" + "2" * 40):
            self.assertEqual(wallets.validate_addresses(), [])


class StatusTests(unittest.IsolatedAsyncioTestCase):
    async def _status(self, eth_hex, usdc_hex):
        async def fake_rpc(client, method, params):
            return eth_hex if method == "eth_getBalance" else usdc_hex

        with patch.object(wallets, "FACILITATOR_ADDRESS", "0x" + "1" * 40), \
             patch.object(wallets, "X402_PAY_TO", "0x" + "2" * 40), \
             patch.object(wallets, "_rpc", fake_rpc):
            return await wallets.wallet_status()

    async def test_healthy_gas_reads_ok(self):
        # 0.05 ETH in wei
        status = await self._status(hex(50_000_000_000_000_000), hex(1_500_000))
        self.assertEqual(status["gas_wallet"]["state"], "ok")
        self.assertAlmostEqual(status["gas_wallet"]["eth"], 0.05)
        self.assertAlmostEqual(status["receiving_wallet"]["usdc"], 1.5)

    async def test_empty_gas_reads_low(self):
        status = await self._status(hex(1_000_000_000_000), hex(0))
        self.assertEqual(status["gas_wallet"]["state"], "low")

    async def test_unreachable_rpc_is_unknown_not_a_crash(self):
        status = await self._status(None, None)
        self.assertEqual(status["gas_wallet"]["state"], "unknown")
        self.assertIsNone(status["gas_wallet"]["eth"])

    async def test_status_never_contains_key_material(self):
        status = await self._status(hex(50_000_000_000_000_000), hex(1))
        flat = repr(status).lower()
        for forbidden in ("private", "secret", "mnemonic", "seed"):
            self.assertNotIn(forbidden, flat)


if __name__ == "__main__":
    unittest.main()


class OnChainVerificationTests(unittest.IsolatedAsyncioTestCase):
    """A recorded settlement must be checkable against Base itself."""

    async def _check(self, rpc_result, rpc_raises=False):
        class FakeResp:
            def json(self_inner):
                return {"jsonrpc": "2.0", "id": 1, "result": rpc_result}

        class FakeClient:
            async def __aenter__(self_inner):
                return self_inner

            async def __aexit__(self_inner, *a):
                return None

            async def post(self_inner, *a, **k):
                if rpc_raises:
                    raise httpx.ConnectError("down")
                return FakeResp()

        with patch.object(wallets.httpx, "AsyncClient", lambda *a, **k: FakeClient()):
            return await wallets.verify_transaction_onchain("0x" + "a" * 64)

    async def test_mined_usdc_transfer_verifies(self):
        ok, detail = await self._check({"blockNumber": "0x2e7", "to": wallets.USDC_CONTRACT})
        self.assertTrue(ok)
        self.assertIn("USDC", detail)

    async def test_hash_unknown_to_chain_is_rejected(self):
        ok, detail = await self._check(None)
        self.assertFalse(ok)
        self.assertIn("unknown", detail)

    async def test_pending_transaction_is_retried_not_rejected(self):
        ok, _ = await self._check({"blockNumber": None, "to": wallets.USDC_CONTRACT})
        self.assertIsNone(ok)

    async def test_unreachable_rpc_never_rejects_a_payment(self):
        ok, detail = await self._check(None, rpc_raises=True)
        self.assertIsNone(ok)
        self.assertIn("unreachable", detail)

    async def test_malformed_hash_is_rejected_without_a_call(self):
        ok, detail = await wallets.verify_transaction_onchain("not-a-hash")
        self.assertFalse(ok)

    async def test_mined_but_wrong_contract_is_flagged_in_detail(self):
        ok, detail = await self._check({"blockNumber": "0x2e7", "to": "0x" + "9" * 40})
        self.assertTrue(ok)
        self.assertIn("rather than the USDC contract", detail)
