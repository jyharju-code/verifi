"""Admin commands, operator only. Authorization by ADMIN_TELEGRAM_ID."""
import functools
import logging
from decimal import Decimal, InvalidOperation

from telegram import Update
from telegram.ext import ContextTypes

from core import config
from core.bot import payments as bot_payments
from core.db.database import get_pool
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
               count(*) FILTER (WHERE status = 'pending') AS waiting_approval,
               COALESCE(sum(earnings - paid_total), 0) AS owed
        FROM associates
        """
    )
    async with db.acquire() as conn:
        scores = await associate_scores(conn)
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
        f"Owed: ${float(assoc['owed']):.2f}",
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


async def _set_instance_price(update: Update, context, column: str, label: str) -> None:
    if not context.args:
        await update.message.reply_text(f"Usage: /{label} $5 [instance], default {DEFAULT_INSTANCE}")
        return
    try:
        value = Decimal(context.args[0].lstrip("$").replace(",", "."))
    except InvalidOperation:
        await update.message.reply_text("Invalid amount. Example: $5 or $0.50")
        return
    instance = context.args[1] if len(context.args) > 1 else DEFAULT_INSTANCE
    db = await get_pool()
    old = await db.fetchrow("SELECT price_per_verify, associate_commission FROM instances WHERE id = $1", instance)
    row = await db.fetchrow(
        f"UPDATE instances SET {column} = $2 WHERE id = $1 RETURNING id, price_per_verify, associate_commission",
        instance,
        value,
    )
    if row is None:
        await update.message.reply_text(f"No such instance: {instance}.")
        return
    from core.audit import audit

    await audit(
        "bot",
        "price_changed",
        {
            "instance": instance,
            "field": column,
            "old": str(old[column]) if old else None,
            "new": str(value),
        },
        actor="admin",
    )
    await update.message.reply_text(
        f"✅ {row['id']}: price ${float(row['price_per_verify']):.2f}, "
        f"commission ${float(row['associate_commission']):.2f}"
    )


@admin_only
async def cmd_hinta(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _set_instance_price(update, context, "price_per_verify", "price")


@admin_only
async def cmd_palkkio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _set_instance_price(update, context, "associate_commission", "commission")


@admin_only
async def cmd_maksa(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(await bot_payments.pending_report())


@admin_only
async def cmd_maksettu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /paid @username $42")
        return
    await update.message.reply_text(await bot_payments.mark_paid(context.args[0], context.args[1]))
