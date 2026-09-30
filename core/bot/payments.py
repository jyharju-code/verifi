"""Bot-side payout logic behind the admin commands /maksa and /maksettu.

Balances are kept per asset and paid per asset, never converted. The awal
CLI sends USDC only, so a ready command is printed for USDC balances; an
EURC balance gets a manual instruction instead.
"""
import logging
from decimal import Decimal, InvalidOperation

from core.audit import audit
from core.bot.handlers.associate import amount_in
from core.db.database import get_pool
from core.payments import payout, settlement

log = logging.getLogger(__name__)

ASSETS = ("USDC", "EURC")


def _method(method: str) -> str:
    return "on Base" if method == "crypto" else "bank transfer"


async def pending_report() -> str:
    """Text for /maksa: who is owed what in which asset, with a ready command when possible."""
    db = await get_pool()
    async with db.acquire() as conn:
        rows = await settlement.pending_balances(conn)
    if not rows:
        return "Nothing to pay. All balances are settled. ✅"
    lines = ["💸 Unpaid balances:\n"]
    for r in rows:
        handle = f"@{r['username']}" if r["username"] else r["name"]
        payable = settlement.Balance(r["id"], r["asset"], 6, Decimal(r["earned"]), Decimal(r["paid"])).payable
        lines.append(f"{handle}: {amount_in(r['pending'], r['asset'])} ({_method(r['payout_method'])})")
        if r["payout_method"] != "crypto" or not r["wallet_address"] or payable <= 0:
            continue
        if r["asset"] == "USDC":
            cmd = payout.awal_command(float(payable), r["wallet_address"])
            lines.append(f"  {' '.join(cmd)}")
        else:
            lines.append(
                f"  Send {payable} {r['asset']} on Base to {r['wallet_address']} by hand "
                f"(awal sends USDC only)."
            )
    lines.append("\nMark as paid: /paid @name 42 [USDC|EURC], USDC by default")
    return "\n".join(lines)


async def mark_paid(username: str, amount_text: str, asset_text: str | None = None) -> str:
    """Handle /maksettu @username 42 [EURC]. Returns the reply text."""
    asset = (asset_text or "USDC").upper()
    if asset not in ASSETS:
        return f"Unknown asset {asset_text}. Use USDC or EURC."
    try:
        amount = Decimal(amount_text.lstrip("$").replace(",", "."))
    except InvalidOperation:
        return "Invalid amount. Use the form /paid @name 42 [USDC|EURC]"
    if amount <= 0:
        return "The amount must be greater than zero."
    if amount != amount.quantize(Decimal("0.01")):
        return "Payouts are recorded in whole cents."

    db = await get_pool()
    assoc = await db.fetchrow(
        "SELECT * FROM associates WHERE lower(username) = lower($1) AND status <> 'removed'",
        username.lstrip("@"),
    )
    if assoc is None:
        return f"No associate found for @{username.lstrip('@')}."

    async with db.acquire() as conn:
        async with conn.transaction():
            # Serialize payouts per associate so two /paid commands cannot both
            # pass the balance check.
            await conn.execute("SELECT id FROM associates WHERE id = $1 FOR UPDATE", assoc["id"])
            balance = next((b for b in await settlement.balances(conn, assoc["id"]) if b.asset == asset), None)
            pending = balance.pending if balance else Decimal("0")
            if amount > pending:
                return f"@{assoc['username']} is only owed {amount_in(pending, asset)}. Payout not recorded."
            await settlement.record_payout(conn, assoc["id"], amount, assoc["payout_method"], asset=asset)

    await audit(
        "bot",
        "payout_recorded",
        {
            "associate_id": assoc["id"],
            "username": assoc["username"],
            "asset": asset,
            "amount": str(amount),
            "amount_usd": str(amount) if asset == "USDC" else None,
            "method": assoc["payout_method"],
        },
        actor="admin",
    )
    log.info("payout recorded: associate=%s amount=%s %s method=%s", assoc["id"], amount, asset, assoc["payout_method"])
    return (
        f"✅ Recorded: {amount_in(amount, asset)} paid to @{assoc['username']} "
        f"({_method(assoc['payout_method'])}).\n"
        f"Remaining owed: {amount_in(pending - amount, asset)}"
    )
