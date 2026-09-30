"""The startup check that configured token metadata matches the chain."""
import unittest
from unittest.mock import patch

from core import pricing, wallets


def _abi_string_hex(text: str) -> str:
    data = text.encode()
    padded = data + b"\x00" * (-len(data) % 32)
    return "0x" + (32).to_bytes(32, "big").hex() + len(data).to_bytes(32, "big").hex() + padded.hex()


def _uint_hex(value: int) -> str:
    return "0x" + value.to_bytes(32, "big").hex()


def _fake_chain(name: str, version: str, decimals: int):
    answers = {
        wallets._SEL_NAME: _abi_string_hex(name),
        wallets._SEL_VERSION: _abi_string_hex(version),
        wallets._SEL_DECIMALS: _uint_hex(decimals),
    }

    async def fake_rpc(client, method, params):
        return answers[params[0]["data"]]

    return fake_rpc


class TokenMetadataTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.eurc = next(a for a in pricing.load_config({}).assets if a.symbol == "EURC")

    async def test_matching_metadata_passes(self):
        with patch.object(wallets, "_rpc", _fake_chain("EURC", "2", 6)):
            ok, detail = await wallets.check_token_metadata(self.eurc)
        self.assertTrue(ok, detail)

    async def test_a_wrong_eip712_name_is_reported(self):
        with patch.object(wallets, "_rpc", _fake_chain("Euro Coin", "2", 6)):
            ok, detail = await wallets.check_token_metadata(self.eurc)
        self.assertFalse(ok)
        self.assertIn("name on chain 'Euro Coin'", detail)

    async def test_wrong_decimals_are_reported(self):
        with patch.object(wallets, "_rpc", _fake_chain("EURC", "2", 18)):
            ok, detail = await wallets.check_token_metadata(self.eurc)
        self.assertFalse(ok)
        self.assertIn("decimals", detail)

    async def test_an_unreachable_chain_is_unknown_not_a_mismatch(self):
        async def down(client, method, params):
            return None

        with patch.object(wallets, "_rpc", down):
            ok, _ = await wallets.check_token_metadata(self.eurc)
        self.assertIsNone(ok)

    def test_abi_string_decoding(self):
        self.assertEqual(wallets._abi_string(_abi_string_hex("USD Coin")), "USD Coin")
        self.assertIsNone(wallets._abi_string("0xzz"))
        self.assertIsNone(wallets._abi_string(None))
