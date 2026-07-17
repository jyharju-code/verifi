import base64
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from core.mcp.server import unlock_verify, verify_claim


class FakeResponse:
    def __init__(self, status_code, body, headers=None):
        self.status_code = status_code
        self._body = body
        self.headers = headers or {}

    def json(self):
        return self._body


class FakeClient:
    def __init__(self, response, calls):
        self.response = response
        self.calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


def encoded_requirement(amount):
    payload = {
        "x402Version": 2,
        "accepts": [{"amount": amount, "network": "eip155:8453"}],
        "resource": {"url": "mcp://tool/verify_claim"},
    }
    return base64.b64encode(json.dumps(payload).encode()).decode()


def context_with_payment(payment=None):
    extra = {"x402/payment": payment} if payment else {}
    return SimpleNamespace(
        request_context=SimpleNamespace(
            meta=SimpleNamespace(model_extra=extra),
        )
    )


class McpPaymentTests(unittest.IsolatedAsyncioTestCase):
    async def test_entry_returns_standard_x402_requirement_inside_mcp(self):
        calls = []
        requirement = encoded_requirement("100000")
        response = FakeResponse(
            402,
            {"error": "payment required"},
            {"PAYMENT-REQUIRED": requirement},
        )
        with patch(
            "core.mcp.server.httpx.AsyncClient",
            return_value=FakeClient(response, calls),
        ):
            result = await verify_claim(
                "Review a claim",
                "The launch date is 22 July",
                "0x1111111111111111111111111111111111111111",
                context_with_payment(),
            )

        self.assertTrue(result.isError)
        self.assertEqual(result.structuredContent["x402Version"], 2)
        self.assertEqual(result.structuredContent["accepts"][0]["amount"], "100000")
        self.assertEqual(result.structuredContent["paymentRequiredHeader"], requirement)
        self.assertEqual(calls[0][1]["headers"], {})

    async def test_entry_forwards_standard_mcp_payment_and_returns_receipt(self):
        calls = []
        payment = {
            "x402Version": 2,
            "accepted": {"amount": "100000", "network": "eip155:8453"},
            "payload": {"signature": "0xsigned"},
        }
        receipt = {"success": True, "transaction": "0xtx", "network": "eip155:8453"}
        encoded_receipt = base64.b64encode(json.dumps(receipt).encode()).decode()
        response = FakeResponse(
            202,
            {"verify_id": "a1b2", "status": "processing"},
            {"PAYMENT-RESPONSE": encoded_receipt},
        )
        with patch(
            "core.mcp.server.httpx.AsyncClient",
            return_value=FakeClient(response, calls),
        ):
            result = await verify_claim(
                "Review a claim",
                "The launch date is 22 July",
                "0x1111111111111111111111111111111111111111",
                context_with_payment(payment),
            )

        self.assertFalse(result.isError)
        self.assertEqual(result.structuredContent["status"], "processing")
        self.assertEqual(result.meta["x402/payment-response"]["transaction"], "0xtx")
        expected_signature = base64.b64encode(
            json.dumps(payment, separators=(",", ":")).encode()
        ).decode()
        self.assertEqual(
            calls[0][1]["headers"],
            {"PAYMENT-SIGNATURE": expected_signature},
        )

    async def test_unlock_uses_same_mcp_payment_flow_for_second_gate(self):
        calls = []
        requirement = encoded_requirement("2900000")
        response = FakeResponse(402, {}, {"PAYMENT-REQUIRED": requirement})
        with patch(
            "core.mcp.server.httpx.AsyncClient",
            return_value=FakeClient(response, calls),
        ):
            result = await unlock_verify("a1b2-c3d4", context_with_payment())

        self.assertTrue(result.isError)
        self.assertEqual(result.structuredContent["accepts"][0]["amount"], "2900000")
        self.assertEqual(calls[0][1]["params"], {"id": "a1b2-c3d4"})

    async def test_manual_signature_remains_available_for_generic_mcp_clients(self):
        calls = []
        response = FakeResponse(202, {"verify_id": "a1b2", "status": "processing"})
        with patch(
            "core.mcp.server.httpx.AsyncClient",
            return_value=FakeClient(response, calls),
        ):
            result = await verify_claim(
                "Review a claim",
                "The launch date is 22 July",
                "0x1111111111111111111111111111111111111111",
                context_with_payment(),
                payment_signature="manual-signed-authorization",
            )

        self.assertFalse(result.isError)
        self.assertEqual(result.structuredContent["status"], "processing")
        self.assertEqual(
            calls[0][1]["headers"],
            {"PAYMENT-SIGNATURE": "manual-signed-authorization"},
        )


if __name__ == "__main__":
    unittest.main()
