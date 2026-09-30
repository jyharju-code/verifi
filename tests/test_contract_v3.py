"""The published contract matches the pricing the code actually runs."""
import json
import unittest
from decimal import Decimal

from core import pricing
from tests.dbutil import REPO

CONTRACT = json.loads((REPO / "docs" / "api-contract.json").read_text())


class ContractMatchesCodeTests(unittest.TestCase):
    def test_prices_windows_and_validity_are_the_code_defaults(self):
        cfg = pricing.load_config({})
        self.assertEqual(CONTRACT["contractVersion"], 3)
        self.assertEqual(CONTRACT["priceBasis"], cfg.price_basis)
        self.assertEqual(Decimal(CONTRACT["admissionPriceEur"]), cfg.admission)
        self.assertEqual(Decimal(CONTRACT["slaUnlockPriceEur"]), cfg.sla_unlock)
        self.assertEqual(Decimal(CONTRACT["graceUnlockPriceEur"]), cfg.grace_unlock)
        self.assertEqual(CONTRACT["slaWindowSeconds"], cfg.sla_seconds)
        self.assertEqual(CONTRACT["graceWindowSeconds"], cfg.grace_seconds)
        self.assertEqual(CONTRACT["humanTimeoutSeconds"], cfg.grace_seconds)
        self.assertEqual(CONTRACT["quoteValidSeconds"], cfg.quote_valid_seconds)

    def test_assets_are_the_configured_tokens_in_offer_order(self):
        cfg = pricing.load_config({})
        self.assertEqual(
            [(a["symbol"], a["address"], a["decimals"]) for a in CONTRACT["assets"]],
            [(a.symbol, a.address, a.decimals) for a in cfg.assets],
        )

    def test_the_canonical_doc_states_the_same_version(self):
        api = (REPO / "docs" / "API.md").read_text()
        html = (REPO / "deploy" / "nginx" / "html" / "docs" / "index.html").read_text()
        self.assertIn("Contract version: 3", api)
        self.assertIn('data-contract-version="3"', html)
