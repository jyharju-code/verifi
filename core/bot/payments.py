"""Bot-side payout logic behind the admin commands /maksa and /maksettu."""
import logging
from decimal import Decimal, InvalidOperation

from core.db.database import get_pool
from core.payments import payout, settlement

log = logging.getLogger(__name__)


def _money(amount) -> str:
    return f"${float(amount):.2f}"


async def pending_report() -> str:
    """Text for /maksa: who is owed what, with a ready awal command when possible."""
    db = await get_pool()
    async with db.acquire() as conn:
        rows = await settlement.pending_balances(conn)
    if not rows:
        return "Nothing to pay. All balances are settled. ✅"
    lines = ["💸 Unpaid balances:\n"]
    for r in rows:
        handle = f"@{r['username']}" if r["username"] else r["name"]
        lines.append(f"{handle}: {_money(r['pending'])} ({'USDC' if r['payout_method'] == 'crypto' else 'bank transfer'})")
        if r["payout_method"] == "crypto" and r["wallet_address"]:
            cmd = payout.awal_command(float(r["pending"]), r["wallet_address"])
            lines.append(f"  {' '.join(cmd)}")
    lines.append("\nMark as paid: /paid @name $amount")
    return "\n".join(lines)


async def mark_paid(username: str, amount_text: str) -> str:
    """Handle /maksettu @username $42. Returns the reply text."""
    try:
        amount = Decimal(amount_text.lstrip("$").replace(",", "."))
    except InvalidOperation:
        return "Invalid amount. Use the form /paid @name $42"
    if amount <= 0:
        return "The amount must be greater than zero."

    db = await get_pool()
    assoc = await db.fetchrow(
        "SELECT * FROM associates WHERE lower(username) = lower($1) AND status <> 'removed'",
        username.lstrip("@"),
    )
    if assoc is None:
        return f"No associate found for @{username.lstrip('@')}."
    pending = assoc["earnings"] - assoc["paid_total"]
    if amount > pending:
        return f"@{assoc['username']} is only owed {_money(pending)}. Payout not recorded."

    async with db.acquire() as conn:
        await settlement.record_payout(conn, assoc["id"], amount, assoc["payout_method"])
    from core.audit import audit

    await audit(
        "bot",
        "payout_recorded",
        {"associate_id": assoc["id"], "username": assoc["username"], "amount_usd": str(amount), "method": assoc["payout_method"]},
        actor="admin",
    )
    log.info("payout recorded: associate=%s amount=%s method=%s", assoc["id"], amount, assoc["payout_method"])
    remaining = pending - amount
    return (
        f"✅ Recorded: {_money(amount)} paid to @{assoc['username']} "
        f"({'USDC' if assoc['payout_method'] == 'crypto' else 'bank transfer'}).\n"
        f"Remaining owed: {_money(remaining)}"
    )
