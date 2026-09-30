"""Callback bodies for contract v3 chains carry the window, never the result."""
import json
import unittest
from datetime import datetime, timezone

from core.webhooks import _public_payload

EURC = "0x60a3E35Cc302bFA44Cb288Bc5a4F316Fdb1adb42"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"


def row(asset=EURC, symbol="EURC", status="accepted", window="grace", amount=1_450_000, credit=False):
    moment = datetime(2026, 10, 1, 13, 12, 40, tzinfo=timezone.utc)
    return {
        "id": "0f3f7d0a-0000-4000-8000-0000000000aa", "status": status, "entry_source": "x402",
        "responded_at": moment, "ready_at": moment, "failure_reason": "human_timeout" if status == "expired" else None,
        "failure_credit_granted": credit, "contract_version": 3,
        "bound_terms": json.dumps({"network": "eip155:8453", "asset": asset, "asset_symbol": symbol,
                                   "asset_decimals": 6, "admission": "100000" if symbol == "EURC" else "120000"}),
        "applied_window": window, "unlock_amount_atomic": amount, "response": "secret answer",
        "sla_deadline": datetime(2026, 10, 1, 10, 0, 2, tzinfo=timezone.utc),
        "grace_deadline": datetime(2026, 10, 2, 9, 0, 2, tzinfo=timezone.utc),
    }


class WebhookV3Tests(unittest.TestCase):
    def test_ready_carries_the_window_and_price_but_not_the_result(self):
        body = _public_payload(row())
        self.assertEqual(body["service_window"]["applied"], "grace")
        self.assertEqual(body["service_window"]["unlock_amount"], "1450000")
        self.assertEqual(body["unlock"]["asset"], EURC)
        self.assertIsNone(body["unlock"]["price_usdc"])
        self.assertTrue(body["unlock_url"].endswith("/verify-unlock?id=0f3f7d0a-0000-4000-8000-0000000000aa"))
        self.assertNotIn("secret answer", json.dumps(body))

    def test_a_usdc_chain_keeps_price_usdc(self):
        body = _public_payload(row(asset=USDC, symbol="USDC", window="sla", amount=3_400_000))
        self.assertEqual(body["unlock"]["price_usdc"], "3.40")

    def test_failed_reports_the_credit_in_the_bound_asset(self):
        body = _public_payload(row(status="expired", credit=True))
        self.assertEqual(body["failure"]["entry_credit"], "next_admission")
        self.assertIsNone(body["failure"]["entry_credit_value_usdc"])
        usdc = _public_payload(row(asset=USDC, symbol="USDC", status="expired", credit=True))
        self.assertEqual(usdc["failure"]["entry_credit_value_usdc"], "0.12")

    def test_a_v2_row_is_unchanged(self):
        v2 = {**row(), "contract_version": 2, "bound_terms": None}
        body = _public_payload(v2)
        self.assertEqual(body["unlock"]["price_usdc"], "2.90")
        self.assertNotIn("service_window", body)
