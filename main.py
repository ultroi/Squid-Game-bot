import asyncio
import logging

from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from bot.config import BOT_TOKEN, LOG_LEVEL
from bot.database import ensure_indexes, close_database
from bot.handlers import lobby
from bot.games import rlgl, dalgona, marbles, bridge, voting, final
from bot.handlers.lobby import restore_lobby_timers
from bot import manager

# A bounded value is safer than concurrent_updates(True), which allows up to
# 256 updates at once in python-telegram-bot's default update processor.
MAX_CONCURRENT_UPDATES = 32

logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
)

# HTTPX INFO logs can include Telegram API URLs containing the bot token.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

log = logging.getLogger("squid-game-bot")


async def post_init(app: Application) -> None:
    await ensure_indexes()
    log.info("Database ready")

    await restore_lobby_timers(app)
    await manager.resume_pending_next_round_gates(app)
    log.info("Pending next-round gates resumed.")


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Unhandled Telegram handler error", exc_info=context.error)


def build_application() -> Application:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is missing. Add BOT_TOKEN=... to your .env file.")

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .concurrent_updates(MAX_CONCURRENT_UPDATES)
        .connection_pool_size(64)
        .pool_timeout(5.0)
        .media_write_timeout(300.0)
        .connect_timeout(30.0)
        .read_timeout(60.0)
        .write_timeout(60.0)
        .build()
    )

    # Commands
    app.add_handler(CommandHandler("start", lobby.start))
    app.add_handler(CommandHandler("enterthehell", lobby.enter_the_hell))
    app.add_handler(CommandHandler("cancelgame", lobby.cancel_game))
    app.add_handler(CommandHandler("stats", lobby.stats_command))

    # Lobby callbacks
    app.add_handler(CallbackQueryHandler(lobby.on_join, pattern=r"^lob:join:"))
    app.add_handler(CallbackQueryHandler(lobby.on_start_game, pattern=r"^lob:start:"))

    # Game callbacks
    app.add_handler(CallbackQueryHandler(rlgl.on_move, pattern=r"^rlgl:"))
    app.add_handler(CallbackQueryHandler(dalgona.on_press, pattern=r"^dal:"))
    app.add_handler(CallbackQueryHandler(marbles.on_hide, pattern=r"^mar:hide:"))
    app.add_handler(CallbackQueryHandler(marbles.on_bet, pattern=r"^mar:bet:"))
    app.add_handler(CallbackQueryHandler(marbles.on_guess, pattern=r"^mar:guess:"))
    app.add_handler(CallbackQueryHandler(bridge.on_pick, pattern=r"^bri:"))
    app.add_handler(CallbackQueryHandler(voting.on_vote, pattern=r"^vot:"))
    app.add_handler(CallbackQueryHandler(final.on_pick, pattern=r"^fin:"))

    # Next-round callbacks
    app.add_handler(CallbackQueryHandler(manager.on_next_round, pattern=r"^nr:enter:"))
    app.add_error_handler(on_error)
    return app


async def async_main() -> None:
    app = build_application()
    await app.initialize()
    try:
        # With this custom lifecycle, invoke post_init explicitly after initialize().
        if app.post_init:
            await app.post_init(app)

        if app.updater is None:
            raise RuntimeError("Telegram polling updater is unavailable.")

        await app.start()
        await app.updater.start_polling(
            drop_pending_updates=True,
            allowed_updates=Update.ALL_TYPES,
        )
        log.info(
            "Bot started; polling Telegram updates (max concurrent updates: %s).",
            MAX_CONCURRENT_UPDATES,
        )
        await asyncio.Event().wait()
    finally:
        if app.updater is not None and app.updater.running:
            await app.updater.stop()
        if app.running:
            await app.stop()
        await app.shutdown()
        close_database()


def main() -> None:
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        log.info("Bot stopped by user.")
    except Exception:
        log.exception("Bot failed to start or stopped unexpectedly.")
        raise


if __name__ == "__main__":
    main()
