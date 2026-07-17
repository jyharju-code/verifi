"""Associate commands. English UI; Finnish command aliases are registered
in bot.py for the original operator."""
import logging
import re

from telegram import Update
from telegram.ext import ContextTypes

from core import notify
from core.db.database import get_pool
from core.payments import settlement
from core.routing import pool as routing_pool

log = logging.getLogger(__name__)

_EVM_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")


def money(amount) -> str:
    return f"${float(amount):.2f}"


async def get_associate(telegram_id: int):
    db = await get_pool()
    return await db.fetchrow("SELECT * FROM associates WHERE telegram_id = $1", telegram_id)


async def require_active(update: Update):
    """Return the associate row when active, otherwise reply and return None."""
    assoc = await get_associate(update.effective_user.id)
    if assoc is None:
        await update.message.reply_text("You are not registered yet. Start with /start.")
        return None
    if assoc["status"] == "pending":
        await update.message.reply_text("Your registration is still waiting for approval.")
        return None
    if assoc["status"] == "removed":
        await update.message.reply_text("Your account has been deactivated.")
        return None
    return assoc


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from core import config

    user = update.effective_user
    db = await get_pool()
    existing = await get_associate(user.id)
    if existing:
        await update.message.reply_text(
            "You are already registered. Mark yourself available with /available."
        )
        return
    # The admin approves everyone else, so the admin is active immediately.
    is_admin = user.id == config.ADMIN_TELEGRAM_ID
    await db.execute(
        """
        INSERT INTO associates (name, username, telegram_id, status)
        VALUES ($1, $2, $3, $4)
        """,
        user.full_name or user.first_name or "unknown",
        (user.username or "").lower() or None,
        user.id,
        "active" if is_admin else "pending",
    )
    log.info("new associate: telegram_id=%s username=%s admin=%s", user.id, user.username, is_admin)
    if is_admin:
        await update.message.reply_text(
            "Welcome to Verifi, admin! 👋\n\n"
            "You are active immediately. Mark yourself available with /available "
            "and queued verifies will come to you.\n\n"
            "Admin commands: /stats, /add, /remove, /price, /commission, /payouts, /paid"
        )
        return
    await update.message.reply_text(
        "Welcome to Verifi! 👋\n\n"
        "Your registration has been received and is waiting for approval.\n\n"
        "Meanwhile you can get ready:\n"
        "1. Set your USDC address: /address 0x...\n"
        "   (No wallet yet? Install Rabby Wallet. It takes two minutes.)\n"
        "2. Or choose bank transfer: /payout bank\n\n"
        "Once approved, mark yourself available with /available."
    )


async def cmd_available(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assoc = await require_active(update)
    if not assoc:
        return
    db = await get_pool()
    await db.execute("UPDATE associates SET available = TRUE WHERE id = $1", assoc["id"])
    await update.message.reply_text("✅ You are now available. New verifies will reach you immediately.")

    # Pull any verifies that arrived while nobody was available.
    async with db.acquire() as conn:
        for verify in await routing_pool.unassigned_pending(conn):
            await routing_pool.assign(conn, verify["id"], assoc["id"])
            msg_id = await notify.send_verify_card(assoc["telegram_id"], verify)
            if msg_id:
                await conn.execute(
                    "UPDATE verifies SET telegram_message_id = $2 WHERE id = $1",
                    verify["id"],
                    msg_id,
                )


async def cmd_busy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assoc = await require_active(update)
    if not assoc:
        return
    db = await get_pool()
    await db.execute("UPDATE associates SET available = FALSE WHERE id = $1", assoc["id"])
    await update.message.reply_text("⏸️ You are now busy. No new verifies until /available.")


async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assoc = await require_active(update)
    if not assoc:
        return
    db = await get_pool()
    async with db.acquire() as conn:
        week = await settlement.week_earnings(conn, assoc["id"])
    pending = assoc["earnings"] - assoc["paid_total"]
    await update.message.reply_text(
        f"💰 Your balance\n\n"
        f"This week: {money(week)}\n"
        f"All-time earned: {money(assoc['earnings'])}\n"
        f"Paid out: {money(assoc['paid_total'])}\n"
        f"Awaiting payout: {money(pending)}\n\n"
        f"Verifies: {assoc['total_paid']} paid, {assoc['total_free']} free"
    )


async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assoc = await require_active(update)
    if not assoc:
        return
    db = await get_pool()
    verifies = await db.fetch(
        """
        SELECT verify_no, instance, status, responded_at, created_at
        FROM verifies
        WHERE associate_id = $1
        ORDER BY created_at DESC
        LIMIT 10
        """,
        assoc["id"],
    )
    payouts = await db.fetch(
        """
        SELECT amount, method, created_at
        FROM payouts
        WHERE associate_id = $1
        ORDER BY created_at DESC
        LIMIT 5
        """,
        assoc["id"],
    )
    lines = ["📋 Recent verifies:"]
    if not verifies:
        lines.append("  (none yet)")
    for v in verifies:
        stamp = (v["responded_at"] or v["created_at"]).strftime("%d.%m. %H:%M")
        lines.append(f"  #V-{v['verify_no']} {v['instance']} {v['status']} {stamp}")
    lines.append("")
    lines.append("💸 Recent payouts:")
    if not payouts:
        lines.append("  (none yet)")
    for p in payouts:
        method = "USDC" if p["method"] == "crypto" else "bank transfer"
        lines.append(f"  {money(p['amount'])} {method} {p['created_at'].strftime('%d.%m.%Y')}")
    await update.message.reply_text("\n".join(lines))


async def cmd_address(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assoc = await get_associate(update.effective_user.id)
    if assoc is None:
        await update.message.reply_text("Register first with /start.")
        return
    if not context.args or not _EVM_ADDRESS.match(context.args[0]):
        await update.message.reply_text(
            "Give a valid EVM address: /address 0x...\n"
            "The address is 42 characters long and starts with 0x."
        )
        return
    db = await get_pool()
    await db.execute(
        "UPDATE associates SET wallet_address = $2, payout_method = 'crypto' WHERE id = $1",
        assoc["id"],
        context.args[0],
    )
    await update.message.reply_text(
        f"✅ USDC address saved: {context.args[0][:6]}...{context.args[0][-4:]}\n"
        f"Payout method: USDC on Base."
    )


async def cmd_payout_method(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assoc = await get_associate(update.effective_user.id)
    if assoc is None:
        await update.message.reply_text("Register first with /start.")
        return
    method = (context.args[0].lower() if context.args else "")
    if method not in ("bank", "crypto"):
        await update.message.reply_text("Choose a payout method: /payout bank or /payout crypto")
        return
    if method == "crypto" and not assoc["wallet_address"]:
        await update.message.reply_text("Set your USDC address first: /address 0x...")
        return
    db = await get_pool()
    await db.execute("UPDATE associates SET payout_method = $2 WHERE id = $1", assoc["id"], method)
    name = "bank transfer" if method == "bank" else "USDC on Base"
    await update.message.reply_text(f"✅ Payout method changed: {name}")
