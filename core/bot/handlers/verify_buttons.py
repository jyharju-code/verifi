"""Verify card buttons and refine replies.

Accept and Reject resolve the verify in one tap. Refine sends a
ForceReply prompt; the associate's reply text becomes the response.
The reply is mapped back to the verify through telegram_message_id,
so replying directly to the original card also works.
"""
import logging

from telegram import ForceReply, Update
from telegram.ext import ContextTypes

from core.audit import audit
from core.bot.handlers.associate import get_associate, money
from core.db.database import get_pool
from core.payments import settlement

log = logging.getLogger(__name__)


async def _resolve(conn, verify, status: str, response: str) -> int:
    """Resolve a pending verify. Returns response time in ms."""
    row = await conn.fetchrow(
        """
        UPDATE verifies
        SET status = $2,
            response = $3,
            responded_at = now(),
            response_time_ms = (EXTRACT(EPOCH FROM (now() - COALESCE(assigned_at, created_at))) * 1000)::int
        WHERE id = $1 AND status = 'pending'
        RETURNING response_time_ms
        """,
        verify["id"],
        status,
        response,
    )
    if row is None:
        raise LookupError("verify already resolved")
    credited = await settlement.credit_for_verify(conn, verify["id"])
    await audit(
        "bot",
        "verify_resolved",
        {
            "verify_no": verify["verify_no"],
            "status": status,
            "response_time_ms": row["response_time_ms"],
            "credited_usd": str(credited),
            "associate_id": verify["associate_id"],
        },
    )
    return row["response_time_ms"]


async def _confirmation(conn, verify, status_word: str, ms: int, associate_id: int) -> str:
    week = await settlement.week_earnings(conn, associate_id)
    return (
        f"✅ Verify #V-{verify['verify_no']} {status_word}. {ms / 1000:.1f} s. Thanks!\n"
        f"This week: {money(week)}"
    )


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    _, verify_id, action = query.data.split("|", 2)
    assoc = await get_associate(update.effective_user.id)
    if assoc is None or assoc["status"] != "active":
        await query.answer("Your account is not active.", show_alert=True)
        return

    db = await get_pool()
    verify = await db.fetchrow(
        "SELECT * FROM verifies WHERE id = $1::uuid AND associate_id = $2",
        verify_id,
        assoc["id"],
    )
    if verify is None:
        await query.answer("This verify is not assigned to you.", show_alert=True)
        return
    if verify["status"] != "pending":
        await query.answer("This verify is already handled.", show_alert=True)
        return

    if action == "refine":
        await query.answer()
        prompt = await query.message.reply_text(
            f"📝 Verify #V-{verify['verify_no']}: write the refined answer "
            f"as a reply to this message.",
            reply_markup=ForceReply(selective=True),
        )
        # Repoint the mapping at the prompt so the ForceReply reply resolves it.
        await db.execute(
            "UPDATE verifies SET telegram_message_id = $2 WHERE id = $1",
            verify["id"],
            prompt.message_id,
        )
        return

    if action not in ("accepted", "rejected"):
        await query.answer()
        return

    async with db.acquire() as conn:
        async with conn.transaction():
            try:
                ms = await _resolve(conn, verify, action, action)
            except LookupError:
                await query.answer("This verify is already handled.", show_alert=True)
                return
        status_word = "accepted" if action == "accepted" else "rejected"
        confirmation = await _confirmation(conn, verify, status_word, ms, assoc["id"])

    mark = "✅" if action == "accepted" else "❌"
    await query.answer()
    await query.edit_message_text(f"{query.message.text}\n\n{mark} {status_word.upper()}")
    await query.message.reply_text(confirmation)
    log.info("verify %s %s by associate %s in %sms", verify["id"], action, assoc["id"], ms)


async def on_refine_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A text reply to a verify card or refine prompt becomes the refined answer."""
    message = update.message
    if message is None or message.reply_to_message is None or not message.text:
        return
    assoc = await get_associate(update.effective_user.id)
    if assoc is None or assoc["status"] != "active":
        return

    db = await get_pool()
    verify = await db.fetchrow(
        """
        SELECT * FROM verifies
        WHERE associate_id = $1 AND telegram_message_id = $2 AND status = 'pending'
        """,
        assoc["id"],
        message.reply_to_message.message_id,
    )
    if verify is None:
        return

    async with db.acquire() as conn:
        async with conn.transaction():
            try:
                ms = await _resolve(conn, verify, "refined", message.text.strip())
            except LookupError:
                await message.reply_text("This verify is already handled.")
                return
        confirmation = await _confirmation(conn, verify, "refined", ms, assoc["id"])

    await message.reply_text(confirmation)
    log.info("verify %s refined by associate %s in %sms", verify["id"], assoc["id"], ms)
