"""USDC payouts on Base via the Coinbase Agentic Wallet CLI (awal).

Phase 1: this module only prepares and prints the command; the operator
pays manually and marks it with /paid. Phase 2: set
PAYOUTS_AUTO=true and send_usdc actually executes the transfer.
"""
import asyncio
import json
import logging
import re

from core import config

log = logging.getLogger(__name__)

_EVM_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")


class PayoutError(Exception):
    pass


def awal_command(amount_usd: float, address: str) -> list[str]:
    if not _EVM_ADDRESS.match(address):
        raise PayoutError(f"Invalid EVM address: {address}")
    if amount_usd <= 0:
        raise PayoutError(f"Invalid amount: {amount_usd}")
    return ["npx", "awal", "send", f"${amount_usd:.2f}", address, "--chain", "base", "--json"]


async def send_usdc(amount_usd: float, address: str) -> dict:
    """Send USDC on Base. Returns the parsed awal JSON result.

    Refuses to run unless PAYOUTS_AUTO=true so Phase 1 can never
    accidentally move funds.
    """
    cmd = awal_command(amount_usd, address)
    if not config.PAYOUTS_AUTO:
        log.info("PAYOUTS_AUTO is off, dry run: %s", " ".join(cmd))
        return {"dry_run": True, "command": " ".join(cmd)}

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise PayoutError(f"awal failed ({proc.returncode}): {stderr.decode(errors='replace')[:500]}")
    try:
        result = json.loads(stdout.decode())
    except json.JSONDecodeError as exc:
        raise PayoutError(f"awal returned non-JSON output: {stdout[:200]!r}") from exc
    log.info("payout sent: %.2f USD to %s tx=%s", amount_usd, address, result.get("transactionHash"))
    return result
