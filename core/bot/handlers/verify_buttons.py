"""Verify card buttons and refine replies.

Accept and Reject resolve the verify in one tap. Refine sends a
ForceReply prompt; the associate's reply text becomes the response.
The reply is mapped back to the verify through telegram_message_id,
so replying directly to the original card also works.
"""
import logging

from telegram import ForceReply, Update
from telegram.ext import ContextTypes

from core import pricing, windows
from core.audit import audit
from core.bot.handlers.associate import get_associate, money
from core.db.database import get_pool
from core.payments import settlement

log = logging.getLogger(__name__)


async def _resolve(conn, verify, status: str, response: str):
    """Resolve a pending verify and decide its unlock price. Returns the row.

    Raises AnswerRefused (a LookupError) when the verify is no longer pending
    or its last window has closed.
    """
    row = await windows.record_answer(conn, verify["id"], status, response)
    credited = await settlement.credit_for_verify(conn, verify["id"])
    v3 = row["contract_version"] == 3
    await audit(
        "bot",
        "verify_resolved",
        {
            "verify_no": verify["verify_no"],
            "verify_id": str(verify["id"]),
            "wallet_address": verify["agent_id"],
            "status": status,
            "agent_status": "ready",
            "response_time_ms": row["response_time_ms"],
            "credited_usd": str(credited),
            "associate_id": verify["associate_id"],
            "contract_version": row["contract_version"],
            "unlock_list_price_usdc": None if v3 else "2.90",
            "unlock_payment_required": verify["entry_source"] != "initial_free",
        },
    )
    if v3:
        await audit(
            "bot",
            "window_applied",
            {
                "verify_no": verify["verify_no"],
                "verify_id": str(verify["id"]),
                "terms_id": verify["terms_id"],
                "admitted_at": verify["admitted_at"].isoformat(),
                "ready_at": row["ready_at"].isoformat(),
                "applied_window": row["applied_window"],
                "asset": row["bound_asset"],
                "unlock_amount_atomic": pricing.atomic_str(row["unlock_amount_atomic"]),
            },
        )
    return row


def _window_line(row) -> str:
    if row["applied_window"] == "sla":
        return "Answered within the SLA window.\n"
    if row["applied_window"] == "grace":
        return "Answered in the grace window.\n"
    return ""


def _refusal(exc: LookupError) -> str:
    if getattr(exc, "reason", None) == "expired":
        return "This verify has expired: its last window closed before your answer."
    return "This verify is already handled."


async def _confirmation(conn, verify, status_word: str, row, associate_id: int) -> str:
    week = await settlement.week_earnings(conn, associate_id)
    ms = row["response_time_ms"]
    return (
        f"✅ Verify #V-{verify['verify_no']} {status_word}. {ms / 1000:.1f} s. Thanks!\n"
        f"{_window_line(row)}"
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
                row = await _resolve(conn, verify, action, action)
            except LookupError as exc:
                await query.answer(_refusal(exc), show_alert=True)
                return
        status_word = "accepted" if action == "accepted" else "rejected"
        confirmation = await _confirmation(conn, verify, status_word, row, assoc["id"])
        ms = row["response_time_ms"]

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
                row = await _resolve(conn, verify, "refined", message.text.strip())
            except LookupError as exc:
                await message.reply_text(_refusal(exc))
                return
        confirmation = await _confirmation(conn, verify, "refined", row, assoc["id"])
        ms = row["response_time_ms"]

    await message.reply_text(confirmation)
    log.info("verify %s refined by associate %s in %sms", verify["id"], assoc["id"], ms)
