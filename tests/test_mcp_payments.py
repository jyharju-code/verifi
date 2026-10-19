import base64
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from core.mcp.server import (
    MCP_CONTRACT_VERSION,
    mcp,
    unlock_verify,
    verifi_info,
    verify_claim,
)


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


def context_with_payment(payment=None, headers=None):
    extra = {"x402/payment": payment} if payment else {}
    return SimpleNamespace(
        request_context=SimpleNamespace(
            meta=SimpleNamespace(model_extra=extra),
            request=SimpleNamespace(headers=headers or {}),
        )
    )


class McpPaymentTests(unittest.IsolatedAsyncioTestCase):
    def test_runtime_version_matches_the_public_contract(self):
        self.assertEqual(MCP_CONTRACT_VERSION, "3.0.1")
        self.assertEqual(mcp._mcp_server.version, MCP_CONTRACT_VERSION)

    async def test_info_is_actionable_for_an_unconfigured_agent(self):
        with patch("core.mcp.server._current_terms", return_value=None):
            info = await verifi_info()
        self.assertEqual(info["mcp_endpoint"], "https://verifi.cloud/mcp")
        self.assertFalse(info["authentication"]["api_key_required"])
        self.assertFalse(info["authentication"]["signup_required"])
        self.assertTrue(info["authentication"]["wallet_required"])
        self.assertEqual(info["callback"]["parameter"], "callback_url")
        self.assertIn("verify.ready", info["callback"]["events"])
        self.assertIn("quickstart-for-buyers", info["x402_http_guide"])

    async def test_info_carries_the_current_terms(self):
        terms = {"version": 1, "terms_id": "st_x", "prices": []}
        with patch("core.mcp.server._current_terms", return_value=terms):
            info = await verifi_info()
        self.assertEqual(info["pricing"]["terms"], terms)
        self.assertEqual(info["contract_version"], 3)

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
        # The call to verify-api is tagged as an MCP-sourced request so its
        # audit row is source mcp, routed by tool name. No payment yet, so no
        # PAYMENT-SIGNATURE.
        self.assertEqual(
            calls[0][1]["headers"],
            {"X-Verifi-Source": "mcp", "X-Verifi-Route": "verify_claim"},
        )

    async def test_payment_requirement_names_a_reachable_public_resource(self):
        """resource.url must be the address the payer can reach, not the internal host.

        Regression: the 402 is produced by an internal call to VERIFY_API_URL
        (http://verify-api:8702), and its resource.url used to be copied straight
        into the public requirement. A payer reading resource.url was handed a host
        that resolves only inside the compose network. Measured on verifi.cloud/mcp
        on 2026-10-18 before the fix.
        """
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

        url = result.structuredContent["resource"]["url"]
        self.assertEqual(url, "https://verifi.cloud/verify")
        self.assertNotIn("verify-api", url)
        self.assertNotIn(":8702", url)
        # The replayed header is untouched: a client sends it back verbatim.
        self.assertEqual(result.structuredContent["paymentRequiredHeader"], requirement)
        decoded = json.loads(base64.b64decode(result.structuredContent["paymentRequiredHeader"]))
        self.assertEqual(decoded["resource"]["url"], "mcp://tool/verify_claim")
        # The terms the payer is quoted are unchanged.
        self.assertEqual(result.structuredContent["accepts"][0]["amount"], "100000")

    async def test_payment_requirement_is_the_signal_the_spec_requires(self):
        """isError true is the payment signal, not a malfunction. Lock that in.

        The x402 MCP transport says: "servers MUST return a tool result with
        isError: true containing the PaymentRequired data", in both
        structuredContent and content[0].text. A future reader may see isError and
        "fix" it to false, which would silently break every x402-aware client: the
        spec's detection step checks structuredContent for x402Version, and a
        client that gates on isError never gets there. This test exists so that
        change has to be argued for rather than made.
        """
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

        # The payment signal the transport mandates.
        self.assertTrue(result.isError)
        # Both carriers present, and the text is the same object JSON-encoded.
        self.assertIn("x402Version", result.structuredContent)
        self.assertEqual(json.loads(result.content[0].text), result.structuredContent)
        # What a client needs to tell "asked to pay" from "broken".
        self.assertIn("accepts", result.structuredContent)
        self.assertTrue(result.structuredContent["accepts"])

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
            {
                "X-Verifi-Source": "mcp",
                "X-Verifi-Route": "verify_claim",
                "PAYMENT-SIGNATURE": expected_signature,
            },
        )

    async def test_entry_forwards_edge_correlation_headers_from_mcp_context(self):
        calls = []
        response = FakeResponse(402, {"error": "payment required"})
        ctx = context_with_payment(headers={
            "x-request-id": "edge-request-123",
            "x-real-ip": "203.0.113.7",
            "x-forwarded-for": "198.51.100.9",
            "x-forwarded-trusted": "1",
        })
        with patch(
            "core.mcp.server.httpx.AsyncClient",
            return_value=FakeClient(response, calls),
        ):
            await verify_claim(
                "Review a claim",
                "The launch date is 22 July",
                "0x1111111111111111111111111111111111111111",
                ctx,
            )

        self.assertEqual(calls[0][1]["headers"]["X-Request-ID"], "edge-request-123")
        self.assertEqual(calls[0][1]["headers"]["X-Real-IP"], "203.0.113.7")
        self.assertEqual(calls[0][1]["headers"]["X-Forwarded-For"], "198.51.100.9")
        self.assertEqual(calls[0][1]["headers"]["X-Forwarded-Trusted"], "1")

    async def test_entry_forwards_callback_url_to_verify_api(self):
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
                callback_url="https://agent.example.com/verifi-events",
            )

        self.assertFalse(result.isError)
        self.assertEqual(
            calls[0][1]["json"]["callback_url"],
            "https://agent.example.com/verifi-events",
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
            {
                "X-Verifi-Source": "mcp",
                "X-Verifi-Route": "verify_claim",
                "PAYMENT-SIGNATURE": "manual-signed-authorization",
            },
        )


if __name__ == "__main__":
    unittest.main()
