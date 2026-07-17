"""Direct Telegram Bot API calls over HTTP.

Used by the core API process, which must send verify cards without
importing the bot application. The bot process itself uses PTB, but the
card format lives here so both sides send identical messages.
"""
import logging

import httpx

from core import config

log = logging.getLogger(__name__)

_API = "https://api.telegram.org/bot{token}/{method}"


def format_verify_card(verify) -> str:
    """The card an associate sees. Layout follows CLAUDE.md."""
    agent = verify["agent_id"] or "unknown"
    if len(agent) > 12:
        agent = f"{agent[:6]}...{agent[-4:]}"
    return (
        f"🧪 NEW VERIFY #V-{verify['verify_no']}\n"
        f"Instance: {verify['instance']}\n"
        f"Intent: {verify['intent']}\n"
        f"Claim: {verify['claim']}\n"
        f"Requester: agent {agent}"
    )


def verify_keyboard(verify_id) -> dict:
    return {
        "inline_keyboard": [
            [
                {"text": "✅ Accept", "callback_data": f"v|{verify_id}|accepted"},
                {"text": "❌ Reject", "callback_data": f"v|{verify_id}|rejected"},
                {"text": "📝 Refine", "callback_data": f"v|{verify_id}|refine"},
            ]
        ]
    }


async def send_message(chat_id: int, text: str, reply_markup: dict | None = None) -> int | None:
    """Send a message, return its Telegram message_id or None on failure."""
    payload: dict = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            _API.format(token=config.TELEGRAM_BOT_TOKEN, method="sendMessage"),
            json=payload,
        )
    data = resp.json()
    if not data.get("ok"):
        log.error("sendMessage failed for chat %s: %s", chat_id, data)
        return None
    return data["result"]["message_id"]


async def send_verify_card(chat_id: int, verify) -> int | None:
    return await send_message(chat_id, format_verify_card(verify), verify_keyboard(verify["id"]))
