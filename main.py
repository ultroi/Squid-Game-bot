import asyncio
import logging
import subprocess
import html
import os
from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)
from telegram.ext import ChatMemberHandler
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


async def _handle_health_request(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Minimal HTTP endpoint required by Render Web Services."""
    try:
        request_line = await asyncio.wait_for(reader.readline(), timeout=5)
        if not request_line:
            return

        parts = request_line.decode("latin-1", errors="replace").strip().split()
        path = parts[1].split("?", 1)[0] if len(parts) >= 2 else "/"

        # Consume headers so the connection is handled cleanly.
        while True:
            line = await asyncio.wait_for(reader.readline(), timeout=5)
            if line in (b"\r\n", b"\n", b""):
                break

        if path == "/health":
            status = "200 OK"
            body = b"OK\n"
        elif path == "/":
            status = "200 OK"
            body = b"Squid Game Bot is running\n"
        else:
            status = "404 Not Found"
            body = b"Not Found\n"

        response = (
            f"HTTP/1.1 {status}\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n\r\n"
        ).encode("ascii") + body
        writer.write(response)
        await writer.drain()
    except (asyncio.TimeoutError, ConnectionError, OSError):
        pass
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except (ConnectionError, OSError):
            pass


async def start_health_server() -> asyncio.AbstractServer:
    """Bind the HTTP health endpoint to the port supplied by Render."""
    port = int(os.environ.get("PORT", "10000"))
    server = await asyncio.start_server(_handle_health_request, "0.0.0.0", port)
    log.info("Health server listening on 0.0.0.0:%s", port)
    return server


async def post_init(app: Application) -> None:
    await ensure_indexes()
    log.info("Database ready")

    await restore_lobby_timers(app)
    await manager.resume_pending_next_round_gates(app)
    cleanup_interrupted_games = getattr(manager, "cleanup_interrupted_games", None)
    if cleanup_interrupted_games is not None:
        await cleanup_interrupted_games(app)
    else:
        log.warning("manager.cleanup_interrupted_games is unavailable; skipping interrupted-game cleanup.")
    log.info("Pending next-round gates resumed.")

    # --- STARTUP DEPLOYMENT NOTIFICATION ---
    try:
        updates_channel = "-1004325447119"

        try:
            commit_msg = subprocess.check_output(
                ["git", "log", "-1", "--pretty=format:%s"],
                stderr=subprocess.STDOUT,
                timeout=3,
            ).decode("utf-8", errors="replace").strip()
            if not commit_msg:
                commit_msg = "System restarted or updated manually."
        except Exception:
            commit_msg = "System restarted or updated manually."

        deploy_message = (
            "🚀 <b>Bot Restarted Successfully</b>\n\n"
            "🛠 <b>Latest Update:</b>\n"
            f"<code>{html.escape(commit_msg)}</code>"
        )

        await app.bot.send_message(
            chat_id=updates_channel,
            text=deploy_message,
            parse_mode="HTML",
        )
        log.info("Startup notification sent to the updates channel.")
    except Exception as exc:
        # A channel notification failure must never stop the bot startup.
        log.warning("Could not send startup notification to channel: %s", exc)


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

    app.add_handler(ChatMemberHandler(lobby.track_group_memberships, ChatMemberHandler.MY_CHAT_MEMBER))

    # Commands
    app.add_handler(CommandHandler("start", lobby.start))
    app.add_handler(CommandHandler("enterthehell", lobby.enter_the_hell))
    app.add_handler(CommandHandler("cancelgame", lobby.cancel_game))
    app.add_handler(CommandHandler("stats", lobby.stats_command))
    app.add_handler(CommandHandler("help", lobby.help_command))
    app.add_handler(CommandHandler("report", lobby.report_command))

    # General callbacks
    app.add_handler(CallbackQueryHandler(lobby.help_callback, pattern=r"^help:"))

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
    # Render Web Services require an open HTTP port. This lightweight server
    # runs alongside Telegram polling and exposes /health for UptimeRobot.
    health_server = await start_health_server()
    app = build_application()
    try:
        await app.initialize()

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
        health_server.close()
        await health_server.wait_closed()


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
