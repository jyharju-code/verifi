"""Wallet safety: watch the money addresses without ever touching a key.

Verifi has two on-chain addresses that matter operationally:

  gas wallet      pays the gas for x402 settlements. Its private key lives
                  only in the facilitator container's environment. If it runs
                  dry, every paid gate stops settling and revenue stops.
  receiving wallet where buyer USDC lands. No key for it exists anywhere in
                  the system.

This module reads balances over a public Base RPC using the public addresses
only. It never reads, derives, logs, or returns a private key. core-api does
not even have FACILITATOR_PRIVATE_KEY in its environment, and
assert_no_private_keys() enforces that at startup.
"""
import logging
import os
import re

import httpx

log = logging.getLogger("verifi.wallets")

EVM_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")

# Public addresses. These are safe to hold, log, and show in the dashboard.
FACILITATOR_ADDRESS = os.environ.get("FACILITATOR_ADDRESS", "").strip()
X402_PAY_TO = os.environ.get("X402_PAY_TO", "").strip()

BASE_RPC_URL = os.environ.get("BASE_RPC_URL", "https://mainnet.base.org")
BASE_EXPLORER = "https://basescan.org/address/"
# USDC on Base mainnet, 6 decimals.
USDC_CONTRACT = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
USDC_DECIMALS = 6
# balanceOf(address)
ERC20_BALANCE_OF = "0x70a08231"

# Below this the gas till can no longer be trusted to settle payments.
GAS_LOW_ETH = float(os.environ.get("GAS_LOW_ETH", "0.002"))
# Never nag more often than this.
GAS_ALERT_INTERVAL_S = int(os.environ.get("GAS_ALERT_INTERVAL_S", str(6 * 60 * 60)))

# Any environment variable whose name looks like a secret must never reach
# core-api. The facilitator holds the only key, in its own container.
_FORBIDDEN_ENV = ("FACILITATOR_PRIVATE_KEY", "EVM_PRIVATE_KEY", "PRIVATE_KEY")


class WalletConfigError(RuntimeError):
    pass


def assert_no_private_keys() -> None:
    """Fail loudly if a private key was mounted into this process.

    core-api only ever needs public addresses. A key here would mean the
    compose file or .env leaked one into the wrong container.
    """
    found = [name for name in _FORBIDDEN_ENV if os.environ.get(name)]
    if found:
        raise WalletConfigError(
            "core-api must not receive private keys, found: " + ", ".join(sorted(found))
        )


def validate_addresses() -> list[str]:
    """Return configuration problems. Empty list means the setup is sane."""
    problems = []
    for label, value in (("FACILITATOR_ADDRESS", FACILITATOR_ADDRESS), ("X402_PAY_TO", X402_PAY_TO)):
        if not value:
            problems.append(f"{label} is not configured, balance cannot be watched")
        elif not EVM_ADDRESS.match(value):
            problems.append(f"{label} is not a valid EVM address")
    return problems


async def _rpc(client: httpx.AsyncClient, method: str, params: list) -> str | None:
    try:
        resp = await client.post(
            BASE_RPC_URL,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        )
        body = resp.json()
    except (httpx.HTTPError, ValueError):
        log.warning("Base RPC call failed: %s", method)
        return None
    if "error" in body:
        log.warning("Base RPC returned an error for %s", method)
        return None
    result = body.get("result")
    return result if isinstance(result, str) else None


def _from_hex(value: str | None, decimals: int) -> float | None:
    if not value:
        return None
    try:
        return int(value, 16) / (10 ** decimals)
    except ValueError:
        return None


async def _eth_balance(client: httpx.AsyncClient, address: str) -> float | None:
    return _from_hex(await _rpc(client, "eth_getBalance", [address, "latest"]), 18)


async def _usdc_balance(client: httpx.AsyncClient, address: str) -> float | None:
    data = ERC20_BALANCE_OF + address[2:].lower().rjust(64, "0")
    result = await _rpc(client, "eth_call", [{"to": USDC_CONTRACT, "data": data}, "latest"])
    return _from_hex(result, USDC_DECIMALS)


TX_HASH = re.compile(r"^0x[0-9a-fA-F]{64}$")


async def verify_transaction_onchain(tx_hash: str) -> tuple[bool | None, str]:
    """Confirm a settlement hash really exists and was mined on Base.

    Returns (verified, detail). verified is True when the transaction is mined,
    False when the chain does not know it, and None when the answer could not
    be obtained. None is not a failure: it means try again later.

    This is deliberately a check after the fact, never a gate. The x402
    middleware and the facilitator are what enforce payment; this exists so a
    hash that was never mined cannot sit in the ledger unnoticed.
    """
    if not TX_HASH.match(tx_hash or ""):
        return False, "not a transaction hash"
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            resp = await client.post(
                BASE_RPC_URL,
                json={
                    "jsonrpc": "2.0", "id": 1,
                    "method": "eth_getTransactionByHash", "params": [tx_hash],
                },
            )
            body = resp.json()
        except (httpx.HTTPError, ValueError):
            return None, "rpc unreachable"
    if "error" in body:
        return None, "rpc error"
    tx = body.get("result")
    if tx is None:
        return False, "unknown to the chain"
    if not tx.get("blockNumber"):
        return None, "pending, not mined yet"
    to = (tx.get("to") or "").lower()
    if to != USDC_CONTRACT.lower():
        return True, f"mined, but sent to {to} rather than the USDC contract"
    return True, "mined, USDC contract"


async def wallet_status() -> dict:
    """Balances and health for the two public addresses.

    Never returns key material. Every field here is safe to show an operator.
    """
    problems = validate_addresses()
    status = {
        "gas_wallet": {
            "label": "Kaasulompakko (facilitator)",
            "purpose": "Maksaa x402-settlementtien kaasun. Avain on vain facilitator-kontissa.",
            "address": FACILITATOR_ADDRESS or None,
            "explorer": (BASE_EXPLORER + FACILITATOR_ADDRESS) if FACILITATOR_ADDRESS else None,
            "eth": None,
            "low_threshold_eth": GAS_LOW_ETH,
            "state": "unknown",
        },
        "receiving_wallet": {
            "label": "Vastaanottava lompakko",
            "purpose": "Tulot laskeutuvat tänne ketjussa. Avainta ei ole palvelimella.",
            "address": X402_PAY_TO or None,
            "explorer": (BASE_EXPLORER + X402_PAY_TO) if X402_PAY_TO else None,
            "usdc": None,
            "state": "unknown",
        },
        "network": "Base mainnet (eip155:8453)",
        "problems": problems,
        "checked_at": None,
    }

    async with httpx.AsyncClient(timeout=10) as client:
        if EVM_ADDRESS.match(FACILITATOR_ADDRESS or ""):
            eth = await _eth_balance(client, FACILITATOR_ADDRESS)
            status["gas_wallet"]["eth"] = eth
            if eth is None:
                status["gas_wallet"]["state"] = "unknown"
            elif eth < GAS_LOW_ETH:
                status["gas_wallet"]["state"] = "low"
            elif eth < GAS_LOW_ETH * 3:
                status["gas_wallet"]["state"] = "warning"
            else:
                status["gas_wallet"]["state"] = "ok"
        if EVM_ADDRESS.match(X402_PAY_TO or ""):
            usdc = await _usdc_balance(client, X402_PAY_TO)
            status["receiving_wallet"]["usdc"] = usdc
            status["receiving_wallet"]["state"] = "unknown" if usdc is None else "ok"

    from datetime import datetime, timezone

    status["checked_at"] = datetime.now(timezone.utc).isoformat()
    return status
