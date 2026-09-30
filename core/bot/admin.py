"""Admin commands, operator only. Authorization by ADMIN_TELEGRAM_ID."""
import functools
import logging
from decimal import Decimal, InvalidOperation

from telegram import Update
from telegram.ext import ContextTypes

from core import config, pricing
from core.bot import payments as bot_payments
from core.bot.handlers.associate import per_asset
from core.db.database import get_pool
from core.payments import settlement
from core.routing.scoring import associate_scores

log = logging.getLogger(__name__)

DEFAULT_INSTANCE = "verify-api"


def admin_only(handler):
    @functools.wraps(handler)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != config.ADMIN_TELEGRAM_ID:
            log.warning("admin command from non-admin %s", update.effective_user.id)
            return
        return await handler(update, context)

    return wrapped


@admin_only
async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db = await get_pool()
    totals = await db.fetchrow(
        """
        SELECT count(*) AS total,
               count(*) FILTER (WHERE status = 'pending') AS pending,
               count(*) FILTER (WHERE status = 'accepted') AS accepted,
               count(*) FILTER (WHERE status = 'rejected') AS rejected,
               count(*) FILTER (WHERE status = 'refined') AS refined,
               count(*) FILTER (WHERE status = 'expired') AS expired,
               count(*) FILTER (WHERE created_at >= date_trunc('week', now())) AS this_week,
               count(*) FILTER (WHERE tier = 'paid') AS paid_tier,
               avg(response_time_ms) FILTER (WHERE response_time_ms IS NOT NULL) AS avg_ms
        FROM verifies
        """
    )
    assoc = await db.fetchrow(
        """
        SELECT count(*) FILTER (WHERE status = 'active') AS active,
               count(*) FILTER (WHERE status = 'active' AND available) AS available,
               count(*) FILTER (WHERE status = 'pending') AS waiting_approval
        FROM associates
        """
    )
    async with db.acquire() as conn:
        scores = await associate_scores(conn)
        owed: dict = {}
        for b in await settlement.balances(conn):
            owed[b.asset] = owed.get(b.asset, Decimal(0)) + max(b.pending, Decimal(0))
    lines = [
        "📊 Verifi statistics\n",
        f"Verifies total: {totals['total']} (this week {totals['this_week']})",
        f"  pending {totals['pending']}, accepted {totals['accepted']}, "
        f"rejected {totals['rejected']}, refined {totals['refined']}, expired {totals['expired']}",
        f"  paid: {totals['paid_tier']}",
        f"  avg response time: {(totals['avg_ms'] or 0) / 1000:.1f} s",
        "",
        f"Associates: {assoc['active']} active, {assoc['available']} available, "
        f"{assoc['waiting_approval']} waiting approval",
        f"Owed: {per_asset(owed)}",
    ]
    if scores:
        lines.append("\nScores (accuracy + speed):")
        for s in scores:
            avg = f"{s['avg_ms'] / 1000:.1f} s" if s["avg_ms"] else "no data"
            lines.append(f"  {s['name']}: {s['score']:.2f} ({s['answered']} answers, {avg})")
    await update.message.reply_text("\n".join(lines))


@admin_only
async def cmd_lisaa(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Usage: /add @username")
        return
    username = context.args[0].lstrip("@").lower()
    db = await get_pool()
    row = await db.fetchrow(
        """
        UPDATE associates SET status = 'active'
        WHERE lower(username) = $1 AND status IN ('pending', 'paused', 'removed')
        RETURNING id, name, telegram_id
        """,
        username,
    )
    if row is None:
        await update.message.reply_text(
            f"No pending registration found for @{username}.\n"
            f"Ask them to send /start to the bot first."
        )
        return
    await update.message.reply_text(f"✅ @{username} ({row['name']}) is now an active associate.")
    try:
        from core import notify

        await notify.send_message(
            row["telegram_id"],
            "🎉 You have been approved as a Verifi associate!\n"
            "Mark yourself available with /available to start receiving verifies.",
        )
    except Exception:
        log.exception("could not notify new associate")


@admin_only
async def cmd_poista(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Usage: /remove @username")
        return
    username = context.args[0].lstrip("@").lower()
    db = await get_pool()
    row = await db.fetchrow(
        """
        UPDATE associates SET status = 'removed', available = FALSE
        WHERE lower(username) = $1 AND status <> 'removed'
        RETURNING name
        """,
        username,
    )
    if row is None:
        await update.message.reply_text(f"No active associate found for @{username}.")
        return
    await update.message.reply_text(f"🗑️ @{username} ({row['name']}) removed.")


@admin_only
async def cmd_hinta(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the current terms. Prices come from the core-api environment."""
    db = await get_pool()
    row = await db.fetchrow("SELECT terms_id, terms FROM pricing_terms ORDER BY issued_at DESC LIMIT 1")
    if row is None:
        await update.message.reply_text("No quote issued yet. The first 402 creates one.")
        return
    terms = pricing.as_json(row["terms"])
    basis = terms["price_basis"]
    windows = terms["windows"]
    lines = [
        f"💶 Terms {row['terms_id']}",
        f"Basis EUR: admission {basis['admission']}, SLA unlock {basis['sla']}"
        + (f", grace unlock {basis['grace']}" if "grace" in basis else ", no grace window"),
        f"SLA window {windows['sla']['within_seconds'] // 60} min"
        + (f", grace {windows['grace']['within_seconds'] // 3600} h" if "grace" in windows else ""),
    ]
    for price in terms["prices"]:
        conv = price.get("conversion")
        amounts = ", ".join(
            f"{k} {pricing.from_atomic(int(price[k]), 6):.2f}" for k in ("admission", "sla", "grace") if k in price
        )
        lines.append(f"{price['asset'][:10]}...: {amounts}" + (f" (1 EUR = {conv['rate']}, {conv['as_of']})" if conv else ""))
    lines.append("Prices change only through ADMISSION_EUR, SLA_UNLOCK_EUR and GRACE_UNLOCK_EUR on core-api.")
    await update.message.reply_text("\n".join(lines))


@admin_only
async def cmd_palkkio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set the responder commission in EUR per SLA answer, for new quotes."""
    if not context.args:
        await update.message.reply_text(f"Usage: /commission 0.50 [instance], EUR per SLA answer, default {DEFAULT_INSTANCE}")
        return
    try:
        value = Decimal(context.args[0].lstrip("$€").replace(",", "."))
    except InvalidOperation:
        await update.message.reply_text("Invalid amount. Example: 0.50")
        return
    from core.api.server import pricing_config

    ceiling = pricing_config().sla_unlock
    if not (Decimal(0) <= value <= ceiling) or value != value.quantize(Decimal("0.01")):
        await update.message.reply_text(f"The commission must be between 0 and {ceiling} EUR, in cents.")
        return
    instance = context.args[1] if len(context.args) > 1 else DEFAULT_INSTANCE
    db = await get_pool()
    old = await db.fetchval("SELECT associate_commission FROM instances WHERE id = $1", instance)
    if old is None:
        await update.message.reply_text(f"No such instance: {instance}.")
        return
    await db.execute("UPDATE instances SET associate_commission = $2 WHERE id = $1", instance, value)
    from core.audit import audit

    await audit(
        "bot",
        "commission_changed",
        {"instance": instance, "old_commission_eur": str(old), "new_commission_eur": str(value)},
        actor="admin",
    )
    await update.message.reply_text(
        f"✅ {instance}: commission {value} EUR per SLA answer, for chains admitted from the next quote on."
    )


@admin_only
async def cmd_maksa(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(await bot_payments.pending_report())


@admin_only
async def cmd_maksettu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /paid @username 42 [USDC|EURC]")
        return
    asset = context.args[2] if len(context.args) > 2 else None
    await update.message.reply_text(await bot_payments.mark_paid(context.args[0], context.args[1], asset))
