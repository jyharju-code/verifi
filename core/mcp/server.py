"""Verifi MCP server: exposes human verification as MCP tools.

Streamable HTTP transport at /mcp, proxied publicly through nginx as
https://verifi.cloud/mcp. Tools call the public Verify API inside the
docker network, so quota, caps, and rules apply identically.

Free and paid chains use the same MCP tools. When a paid gate returns an x402
requirement, the tool returns the exact PAYMENT-REQUIRED value. The caller
signs it with its wallet and repeats the same tool call with payment_signature.
Private keys never pass through Verifi.

Run: python -m core.mcp.server
"""
import base64
import json
import os

import httpx
from mcp.server.fastmcp import Context, FastMCP
from mcp.types import CallToolResult, TextContent

VERIFY_API = os.environ.get("VERIFY_API_URL", "http://verify-api:8702")
MCP_PORT = int(os.environ.get("MCP_PORT", "8704"))
MCP_PAYMENT_META_KEY = "x402/payment"
MCP_PAYMENT_RESPONSE_META_KEY = "x402/payment-response"

mcp = FastMCP(
    "verifi",
    host="0.0.0.0",
    port=MCP_PORT,
    stateless_http=True,
    instructions=(
        "Verifi sends your claim to a real human who answers accept, reject, "
        "or a refined free-text correction. Every chain has two gates. Use "
        "verify_claim, poll get_verify until ready or failed, then use "
        "unlock_verify when ready. The first 5 complete chains per wallet are "
        "free at both gates. After that, x402-aware MCP clients can sign and "
        "repeat paid tool calls automatically. Generic clients can pass the "
        "signed authorization as payment_signature. Never send a private key. "
        "A human reads every request: do not spam."
    ),
)


def _decode_payment_required(value: str | None) -> dict | None:
    """Decode an x402 header for agents that prefer structured requirements."""
    if not value:
        return None
    try:
        padded = value + "=" * (-len(value) % 4)
        return json.loads(base64.b64decode(padded).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def _payment_from_context(ctx: Context | None) -> dict | None:
    """Read the standard x402 payment payload from MCP request metadata."""
    if ctx is None:
        return None
    try:
        request_meta = ctx.request_context.meta
        if request_meta is not None and request_meta.model_extra:
            payment = request_meta.model_extra.get(MCP_PAYMENT_META_KEY)
            return payment if isinstance(payment, dict) else None
    except (ValueError, AttributeError):
        pass
    return None


def _encode_payment_signature(payment: dict | None) -> str | None:
    """Translate the standard MCP payment metadata into an x402 HTTP header."""
    if not payment:
        return None
    encoded = json.dumps(payment, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return base64.b64encode(encoded).decode("ascii")


def _payment_result(resp: httpx.Response, *, tool: str, price: str) -> CallToolResult:
    """Translate HTTP x402 responses to the standard MCP payment shape."""
    body = resp.json()
    if resp.status_code == 402:
        required = resp.headers.get("PAYMENT-REQUIRED")
        payment_required = _decode_payment_required(required) or {
            "x402Version": 2,
            "accepts": [],
            "error": f"{price} payment is required for {tool}",
        }
        payment_required["error"] = body.get(
            "error",
            f"{price} payment is required. Sign it with the requester wallet and retry {tool}.",
        )
        payment_required["paymentRequiredHeader"] = required
        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps(payment_required))],
            structuredContent=payment_required,
            isError=True,
        )

    result = dict(body)
    payment_response = resp.headers.get("PAYMENT-RESPONSE")
    response_meta = None
    if payment_response:
        result["payment_response_header"] = payment_response
        decoded_response = _decode_payment_required(payment_response)
        response_meta = {
            MCP_PAYMENT_RESPONSE_META_KEY: decoded_response or {"encoded": payment_response}
        }
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(result))],
        structuredContent=result,
        isError=resp.status_code >= 400,
        _meta=response_meta,
    )


@mcp.tool()
async def verify_claim(
    intent: str,
    claim: str,
    agent_id: str,
    ctx: Context,
    payment_signature: str | None = None,
) -> CallToolResult:
    """Ask a real human to verify a claim.

    intent: what your agent is trying to do (max 2000 chars).
    claim: the claim a human should verify (max 4000 chars).
    agent_id: your wallet address (0x + 40 hex). Grants 5 free verifies.
    payment_signature: optional manual compatibility input. Standard x402-aware
    MCP clients send the signed payment through request metadata automatically.
    Returns status "processing" with a verify_id. Poll get_verify at the
    returned interval until status is "ready" or "failed". If ready, call
    unlock_verify. Only one active verify per agent_id at a time.
    """
    signature = payment_signature or _encode_payment_signature(_payment_from_context(ctx))
    headers = {"PAYMENT-SIGNATURE": signature} if signature else {}
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{VERIFY_API}/verify",
            json={"intent": intent, "claim": claim, "agent_id": agent_id},
            headers=headers,
        )
    return _payment_result(resp, tool="verify_claim", price="0.10 USDC")


@mcp.tool()
async def get_verify(verify_id: str) -> dict:
    """Poll until ready or failed. Honor retry_after_seconds while processing."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(f"{VERIFY_API}/verify/{verify_id}")
    return resp.json()


@mcp.tool()
async def unlock_verify(
    verify_id: str,
    ctx: Context,
    payment_signature: str | None = None,
) -> CallToolResult:
    """Pass gate 2 for a ready chain and return the human result.

    The first five chains per wallet are free at both gates. After that, omit
    payment_signature first. Standard x402-aware MCP clients handle the payment
    request and retry through MCP metadata automatically. Generic clients can
    pass the resulting x402 signature manually. Never pass a private key.
    """
    signature = payment_signature or _encode_payment_signature(_payment_from_context(ctx))
    headers = {"PAYMENT-SIGNATURE": signature} if signature else {}
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{VERIFY_API}/verify-unlock",
            params={"id": verify_id},
            headers=headers,
        )
    return _payment_result(resp, tool="unlock_verify", price="2.90 USDC")


@mcp.tool()
def verifi_info() -> dict:
    """Service description, pricing, and rules."""
    return {
        "service": "Verifi: verified human loops for AI agents",
        "url": "https://verifi.cloud",
        "docs": "https://verifi.cloud/docs/",
        "pricing": {
            "free": "5 complete chains per wallet, entry and unlock both free",
            "paid": "0.10 USDC entry, then a separate 2.90 USDC unlock, total 3.00 USDC",
        },
        "mcp_payment": (
            "Paid gates return a standard x402 MCP payment requirement. Compatible "
            "clients sign and retry automatically; generic clients may use "
            "payment_signature."
        ),
        "rules": [
            "One active verify per agent_id at a time",
            "Poll processing verifies until ready or failed",
            "Ready results require the separate unlock action",
            "Unanswered verifies expire in 60 minutes",
            "A failed admitted verify grants one 0.10 USDC entry credit",
            "A real human reads every request: do not spam",
        ],
    }


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
