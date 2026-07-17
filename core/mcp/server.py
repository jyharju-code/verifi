"""Verifi MCP server: exposes human verification as MCP tools.

Streamable HTTP transport at /mcp, proxied publicly through nginx as
https://verifi.cloud/mcp. Tools call the public Verify API inside the
docker network, so quota, caps, and rules apply identically.

Scope v1: five full-free chains per wallet, polling, and entitlement
unlock. The x402-paid tier runs over plain HTTP as documented at
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
        "or a refined free-text correction. Every chain has two gates. Use "
        "verify_claim, poll get_verify until ready or failed, then use "
        "unlock_verify when ready. The first 5 complete chains per wallet are "
        "free at both gates. A human reads every request: do not spam."
    ),
)


@mcp.tool()
async def verify_claim(intent: str, claim: str, agent_id: str) -> dict:
    """Ask a real human to verify a claim.

    intent: what your agent is trying to do (max 2000 chars).
    claim: the claim a human should verify (max 4000 chars).
    agent_id: your wallet address (0x + 40 hex). Grants 5 free verifies.
    Returns status "processing" with a verify_id. Poll get_verify at the
    returned interval until status is "ready" or "failed". If ready, call
    unlock_verify. Only one active verify per agent_id at a time.
    """
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{VERIFY_API}/verify",
            json={"intent": intent, "claim": claim, "agent_id": agent_id},
        )
    body = resp.json()
    if resp.status_code == 402:
        return {
            "error": "free quota used",
            "detail": "Your 5 full-free chains are used. Paid HTTP uses 0.10 USDC for entry and 2.90 USDC for result unlock: https://verifi.cloud/docs/",
        }
    return body


@mcp.tool()
async def get_verify(verify_id: str) -> dict:
    """Poll until ready or failed. Honor retry_after_seconds while processing."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(f"{VERIFY_API}/verify/{verify_id}")
    return resp.json()


@mcp.tool()
async def unlock_verify(verify_id: str) -> dict:
    """Pass gate 2 for a ready full-free chain and return the human result.

    The first five chains per wallet are free at both gates. A ready chain
    funded by x402 or a failure credit requires the separate 2.90 USDC HTTP
    payment described at https://verifi.cloud/docs/.
    """
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(f"{VERIFY_API}/verify-unlock", params={"id": verify_id})
    return resp.json()


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
