"""Verifi associate bot. One bot, two roles: associate and admin.

Run from the repo root:
    python -m core.bot.bot

BOT_MODE=webhook in production (receives updates at PUBLIC_BASE_URL/bot),
BOT_MODE=polling for local development.

Commands are English; Finnish aliases are kept for the original operator.
"""
import logging

from telegram import BotCommand, Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from core import config
from core.bot import admin
from core.bot.handlers import associate, verify_buttons
from core.db.database import close_pool, get_pool

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
# httpx logs full request URLs at INFO. Telegram Bot API URLs contain the bot
# token, so dependency request logs must never inherit the application level.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger("verifi.bot")

COMMANDS = [
    BotCommand("start", "Register as an associate"),
    BotCommand("available", "Mark yourself available"),
    BotCommand("busy", "Mark yourself busy"),
    BotCommand("balance", "Show your earnings"),
    BotCommand("history", "Recent verifies and payouts"),
    BotCommand("address", "Set your USDC address"),
    BotCommand("payout", "Choose payout method: bank or crypto"),
]


async def _post_init(app: Application) -> None:
    await get_pool()
    await app.bot.set_my_commands(COMMANDS)
    log.info("bot ready, mode=%s", config.BOT_MODE)


async def _post_shutdown(app: Application) -> None:
    await close_pool()


def build_application() -> Application:
    app = (
        ApplicationBuilder()
        .token(config.TELEGRAM_BOT_TOKEN)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )

    # Associate commands (English primary, Finnish alias)
    app.add_handler(CommandHandler("start", associate.cmd_start))
    app.add_handler(CommandHandler(["available", "vapaa"], associate.cmd_available))
    app.add_handler(CommandHandler(["busy", "varattu"], associate.cmd_busy))
    app.add_handler(CommandHandler(["balance", "saldoni"], associate.cmd_balance))
    app.add_handler(CommandHandler(["history", "historia"], associate.cmd_history))
    app.add_handler(CommandHandler(["address", "osoite"], associate.cmd_address))
    app.add_handler(CommandHandler(["payout", "maksu"], associate.cmd_payout_method))

    # Admin commands (English primary, Finnish alias)
    app.add_handler(CommandHandler("stats", admin.cmd_stats))
    app.add_handler(CommandHandler(["add", "lisaa"], admin.cmd_lisaa))
    app.add_handler(CommandHandler(["remove", "poista"], admin.cmd_poista))
    app.add_handler(CommandHandler(["price", "hinta"], admin.cmd_hinta))
    app.add_handler(CommandHandler(["commission", "palkkio"], admin.cmd_palkkio))
    app.add_handler(CommandHandler(["payouts", "maksa"], admin.cmd_maksa))
    app.add_handler(CommandHandler(["paid", "maksettu"], admin.cmd_maksettu))

    # Verify buttons and refine replies
    app.add_handler(CallbackQueryHandler(verify_buttons.on_button, pattern=r"^v\|"))
    app.add_handler(
        MessageHandler(filters.TEXT & filters.REPLY & ~filters.COMMAND, verify_buttons.on_refine_reply)
    )
    return app


def main() -> None:
    if not config.TELEGRAM_BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set")
    app = build_application()
    if config.BOT_MODE == "webhook":
        app.run_webhook(
            listen=config.BOT_LISTEN,
            port=config.BOT_PORT,
            url_path="bot",
            webhook_url=f"{config.PUBLIC_BASE_URL}/bot",
            secret_token=config.TELEGRAM_WEBHOOK_SECRET or None,
            allowed_updates=Update.ALL_TYPES,
        )
    else:
        app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
