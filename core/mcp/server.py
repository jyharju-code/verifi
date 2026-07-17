"""Verifi MCP server: exposes human verification as MCP tools.

Streamable HTTP transport at /mcp, proxied publicly through nginx as
https://verifi.cloud/mcp. Tools call the public Verify API inside the
docker network, so quota, caps, and rules apply identically.

Scope v1: the free tier (5 verifies per wallet address) and result
polling. The x402-paid tier runs over plain HTTP as documented at
https://verifi.cloud/docs/ ; x402-over-MCP can be added when agents ask
for it.

Run: python -m core.mcp.server
"""
import os

import httpx
from mcp.server.fastmcp import FastMCP

VERIFY_API = os.environ.get("VERIFY_API_URL", "http://verify-api:8702")
MCP_PORT = int(os.environ.get("MCP_PORT", "8704"))

mcp = FastMCP(
    "verifi",
    host="0.0.0.0",
    port=MCP_PORT,
    stateless_http=True,
    instructions=(
        "Verifi sends your claim to a real human who answers accept, reject, "
        "or a refined free-text correction. Use verify_claim for new claims "
        "and get_verify to poll pending ones. Free tier: 5 verifies per "
        "wallet address. A human reads every request: do not spam."
    ),
)


@mcp.tool()
async def verify_claim(intent: str, claim: str, agent_id: str, wait_seconds: int = 55) -> dict:
    """Ask a real human to verify a claim.

    intent: what your agent is trying to do (max 2000 chars).
    claim: the claim a human should verify (max 4000 chars).
    agent_id: your wallet address (0x + 40 hex). Grants 5 free verifies.
    wait_seconds: how long to wait synchronously (0-110, default 55).

    Returns verdict "true" | "false" | "refined" (explanation carries the
    refined text), or status "pending" with a verify_id to poll via
    get_verify. Only one pending verify per agent_id at a time.
    """
    async with httpx.AsyncClient(timeout=wait_seconds + 15) as client:
        resp = await client.post(
            f"{VERIFY_API}/verify",
            params={"wait": max(0, min(110, wait_seconds))},
            json={"intent": intent, "claim": claim, "agent_id": agent_id},
        )
    body = resp.json()
    if resp.status_code == 402:
        return {
            "error": "free quota used",
            "detail": "Your 5 free verifies are used. The paid tier ($0.10 via x402, USDC on Base) runs over plain HTTP: https://verifi.cloud/docs/",
        }
    return body


@mcp.tool()
async def get_verify(verify_id: str) -> dict:
    """Poll a verify by id. Returns status, verdict, explanation, and timing."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(f"{VERIFY_API}/verify/{verify_id}")
    return resp.json()


@mcp.tool()
def verifi_info() -> dict:
    """Service description, pricing, and rules."""
    return {
        "service": "Verifi: verified human loops for AI agents",
        "url": "https://verifi.cloud",
        "docs": "https://verifi.cloud/docs/",
        "pricing": {
            "free": "5 verifies per wallet address (agent_id)",
            "paid": "$0.10 per verify via x402 (USDC on Base, eip155:8453), over plain HTTP",
        },
        "rules": [
            "One pending verify per agent_id at a time",
            "Unanswered verifies expire in 60 minutes",
            "An expired paid verify automatically grants one free verify as credit",
            "A real human reads every request: do not spam",
        ],
    }


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
