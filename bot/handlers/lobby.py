import asyncio
import logging
import os
import time
from datetime import datetime, timezone
import html
from telegram import Update
from telegram.error import Forbidden, TelegramError
from telegram.ext import ContextTypes
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram import ChatMember, ChatMemberUpdated
from .. import config, runtime, ui
from ..database import matches
from ..manager import advance, safe_edit, safe_answer, refresh_lobby_scoreboard
from ..repository import (
    upsert_user,
    create_match,
    get_match,
    save_match,
    get_active_match,
    get_welcome_video_file_id,
    save_welcome_video_file_id,
    clear_welcome_video_file_id,
    get_bot_stats,
    is_user_in_other_active_match,
)

log = logging.getLogger(__name__)
_welcome_video_lock = asyncio.Lock()
_lobby_create_locks: dict[int, asyncio.Lock] = {}


def extract_status_change(chat_member_update: ChatMemberUpdated) -> tuple[bool, bool] | None:
    """Extracts whether the bot's active status changed."""
    status_change = chat_member_update.difference().get("status")
    old_is_member, new_is_member = chat_member_update.difference().get("is_member", (None, None))

    if status_change is None:
        return None

    old_status, new_status = status_change
    was_member = old_status in [
        ChatMember.MEMBER,
        ChatMember.OWNER,
        ChatMember.ADMINISTRATOR,
    ] or (old_status == ChatMember.RESTRICTED and old_is_member is True)
    is_member = new_status in [
        ChatMember.MEMBER,
        ChatMember.OWNER,
        ChatMember.ADMINISTRATOR,
    ] or (new_status == ChatMember.RESTRICTED and new_is_member is True)

    return was_member, is_member

async def track_group_memberships(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Updates the database when the bot is added to or removed from a group."""
    result = extract_status_change(update.my_chat_member)
    if result is None:
        return
    
    was_member, is_member = result
    chat = update.effective_chat
    
    # We only care about groups and supergroups
    if not chat or chat.type not in ["group", "supergroup"]:
        return

    try:
        from ..database import groups
        if not was_member and is_member:
            # Bot was added to a group
            await groups.update_one(
                {"_id": chat.id},
                {"$set": {"active": True, "title": chat.title}},
                upsert=True
            )
            log.info("Bot added to group: %s (%s)", chat.title, chat.id)
        elif was_member and not is_member:
            # Bot was removed or kicked
            await groups.update_one(
                {"_id": chat.id},
                {"$set": {"active": False}}
            )
            log.info("Bot removed from group: %s (%s)", chat.title, chat.id)
    except Exception:
        log.exception("Failed to update group membership tracker for %s", chat.id)



async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if message is None or chat is None or user is None:
        return

    await upsert_user(user)

    # Dynamically fetch the bot's username to generate the Add to Group link
    bot_username = context.bot.username
    if not bot_username:
        bot_username = (await context.bot.get_me()).username

    # Create the inline keyboard with the new buttons
    keyboard = InlineKeyboardMarkup([
        # Row 1
        [InlineKeyboardButton("➕ Add to your Group", url=f"https://t.me/{bot_username}?startgroup=true")],
        # Row 2 (Two buttons side-by-side)
        [
            InlineKeyboardButton("Main GC", url="https://t.me/SquidGameBotMC"),
            InlineKeyboardButton("Updates", url="https://t.me/SquidGameBotUpdates")
        ]
    ])

    async with _welcome_video_lock:
        try:
            file_id = await get_welcome_video_file_id()
        except Exception:
            log.exception("Could not read welcome video cache from MongoDB")
            file_id = None

        if file_id:
            try:
                await message.reply_video(
                    video=file_id,
                    caption=config.WELCOME_CAPTION,
                    parse_mode="HTML",
                    reply_markup=keyboard,  # <-- Added keyboard
                )
                return
            except TelegramError:
                log.exception("Cached welcome video failed; will try uploading local file")
                try:
                    await clear_welcome_video_file_id()
                except Exception:
                    pass

        video_path = config.WELCOME_VIDEO
        if os.path.isfile(video_path):
            try:
                with open(video_path, "rb") as video:
                    sent = await message.reply_video(
                        video=video,
                        caption=config.WELCOME_CAPTION,
                        parse_mode="HTML",
                        reply_markup=keyboard,  # <-- Added keyboard
                    )
                if sent.video:
                    try:
                        await save_welcome_video_file_id(sent.video.file_id)
                    except Exception:
                        pass
                return
            except (OSError, TelegramError):
                log.exception("Welcome video upload failed")
        else:
            log.error("Welcome video file not found: %s", video_path)

    await message.reply_text(
        config.WELCOME_CAPTION, 
        parse_mode="HTML",
        reply_markup=keyboard  # <-- Added keyboard
    )


# ---------------- lobby ----------------

def _lobby_seconds_left(match) -> int:
    """Seconds remaining on the lobby's fixed join deadline."""
    deadline = match.get("lobby_deadline")
    if deadline is None:
        # A missing deadline must not reset the countdown on every poll.
        created_at = match.get("created_at")
        if isinstance(created_at, datetime):
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            deadline = created_at.timestamp() + int(config.LOBBY_SECONDS)
        else:
            return 0
    return max(0, int(float(deadline) - time.time()))


def _lobby_text(match, seconds_left=None) -> str:
    if seconds_left is None:
        seconds_left = _lobby_seconds_left(match)
    return ui.render_lobby(
        match,
        seconds_left=seconds_left,
        min_players=config.MIN_PLAYERS,
        max_players=config.MAX_PLAYERS,
    )


async def _refresh_lobby(context, match, seconds_left=None):
    if not match or not match.get("lobby_message_id"):
        return
    mid = str(match.get("_id", match.get("id", "")))
    if not mid:
        return
    if seconds_left is None:
        seconds_left = _lobby_seconds_left(match)
    can_start = len(match.get("players", [])) >= config.MIN_PLAYERS
    try:
        await safe_edit(
            context.bot,
            match["chat_id"],
            match["lobby_message_id"],
            _lobby_text(match, seconds_left),
            reply_markup=ui.lobby_keyboard(mid, can_start),
        )
    except Exception:
        log.exception("Could not refresh lobby message for match %s", mid)


async def enter_the_hell(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if message is None or chat is None or user is None:
        return
    if chat.type == "private":
        await message.reply_text("👥 Use this command inside a GROUP!")
        return

    # Avoid duplicate lobbies when two /enterthehell commands arrive together.
    lock = _lobby_create_locks.setdefault(chat.id, asyncio.Lock())
    async with lock:
        existing = await get_active_match(chat.id)
        if existing:
            await message.reply_text("⚠️ A game is already running in this group!")
            return

        match = await create_match(chat.id, user.id)
        mid = str(match["_id"])
        match["lobby_deadline"] = time.time() + int(config.LOBBY_SECONDS)

        try:
            msg = await message.reply_text(
                _lobby_text(match, config.LOBBY_SECONDS),
                reply_markup=ui.lobby_keyboard(mid, len(match.get("players", [])) >= config.MIN_PLAYERS),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            match["lobby_message_id"] = msg.message_id
            await save_match(match)
        except Exception:
            log.exception("Could not create lobby card for match %s", mid)
            match["stage"] = "finished"
            try:
                await save_match(match)
            except Exception:
                log.exception("Could not mark failed lobby %s as finished", mid)
            raise
        runtime.add_task(mid, asyncio.create_task(_lobby_countdown(context, mid), name=f"lobby:{mid}"))


async def on_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    parts = (q.data or "").split(":", 2)
    if len(parts) != 3 or parts[0] != "lob" or parts[1] != "join":
        await safe_answer(q, "Invalid lobby button.", True)
        return
    mid = parts[2]
    uid = q.from_user.id
    rt = runtime.get(mid)

    async with rt.setdefault("lobby_lock", asyncio.Lock()):
        match = await get_match(mid)
        if not match or match.get("stage") != "lobby":
            response = ("Lobby is closed.", True)
        elif await is_user_in_other_active_match(uid, mid): 
            response = ("⚠️ You are already in another active game!", True)
        elif _lobby_seconds_left(match) <= 0:
            response = ("Lobby timer has expired. Please wait for it to close.", True)
        elif any(p.get("id") == uid for p in match.get("players", [])):
            response = ("You're already registered! 👥", False)
        # --- NEW CHECK: PREVENT JOINING MULTIPLE LOBBIES ---
        elif await matches.find_one({"stage": "lobby", "_id": {"$ne": match.get("_id")}, "players.id": uid}):
            response = ("⚠️ You are already waiting in another lobby! You can only join one at a time.", True)
        # ---------------------------------------------------
        elif len(match.get("players", [])) >= config.MAX_PLAYERS:
            response = (f"Game is full ({config.MAX_PLAYERS})!", True)
        else:
            try:
                welcome_message = await context.bot.send_message(
                    chat_id=uid,
                    text=(
                        "✅ You joined Squid Game! "
        
                    ),
                )
            except Forbidden:
                me = await context.bot.get_me()
                response = (f"⚠️ First, open @{me.username} in PM and press /start, then come back and join again!", True)
            except Exception:
                log.exception("Failed to send DM to Telegram user %s", uid)
                response = ("⚠️ I couldn't message you. Open the bot in PM, press /start, and try again.", True)
            else:
                # Sending the welcome DM may cross the deadline; never admit a
                # player after timeout merely because Telegram responded slowly.
                if _lobby_seconds_left(match) <= 0:
                    response = ("Lobby timer has expired. Please wait for it to close.", True)
                    match = None
                if match is not None:
                    await upsert_user(q.from_user)
                    emoji = config.PLAYER_EMOJIS[len(match.get("players", [])) % len(config.PLAYER_EMOJIS)]
                    match.setdefault("players", []).append({
                        "id": uid,
                        "name": q.from_user.full_name or q.from_user.first_name or str(uid),
                        "emoji": emoji,
                        "alive": True,
                        "dm_mid": None,
                        "pos": None,
                        "reached": False,
                    })
                    await save_match(match)
                    response = (f"You're in! {emoji}", False)

    await safe_answer(q, response[0], response[1])
    if response[0].startswith("You're in!"):
        match = await get_match(mid)
        if match:
            await _refresh_lobby(context, match)


async def on_start_game(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    parts = (q.data or "").split(":", 2)
    if len(parts) != 3 or parts[0] != "lob" or parts[1] != "start":
        await safe_answer(q, "Invalid start button.", True)
        return
    mid = parts[2]
    rt = runtime.get(mid)
    start_error = None
    response = None

    async with rt.setdefault("lobby_lock", asyncio.Lock()):
        match = await get_match(mid)
        if not match or match.get("stage") != "lobby":
            response = ("Lobby is closed.", True)
        elif q.from_user.id != match.get("host_id", match.get("created_by")):
            response = ("Only the host can start the game!", True)
        elif len(match.get("players", [])) < config.MIN_PLAYERS:
            response = (f"Need at least {config.MIN_PLAYERS} players!", True)
        else:
            runtime.cancel_tasks(mid)
            await safe_answer(q, "Game starting! 🔺")
            response = None  # callback is answered before potentially slow round setup
            try:
                await advance(context, mid)
            except Exception:
                log.exception("Failed to start game via button for match %s", mid)
                start_error = "⚠️ <b>FAILED TO START GAME</b>\n\nThe round could not initialize. Start a new lobby with /enterthehell."
                latest = await get_match(mid)
                if latest and latest.get("stage") != "finished":
                    latest["stage"] = "finished"
                    await save_match(latest)
                    runtime.clear(mid)
                    await refresh_lobby_scoreboard(context, mid, force=True)
                    try:
                        await safe_edit(context.bot, latest["chat_id"], latest.get("round_message_id") or latest.get("lobby_message_id"), start_error, reply_markup=ui.kb([]))
                    except Exception:
                        log.exception("Could not display failed-start notice for match %s", mid)

    if response is not None:
        await safe_answer(q, response[0], response[1])


async def _lobby_countdown(context, mid):
    """Refresh the lobby until its persisted deadline, then transition once."""
    try:
        while True:
            match = await get_match(mid)
            if not match or match.get("stage") != "lobby":
                return
            remaining = _lobby_seconds_left(match)
            if remaining <= 0:
                break
            await _refresh_lobby(context, match, remaining)
            await asyncio.sleep(min(5, remaining))
    except asyncio.CancelledError:
        return
    except Exception:
        log.exception("Unexpected error in lobby countdown loop for match %s", mid)
        return

    rt = runtime.get(mid)
    async with rt.setdefault("lobby_lock", asyncio.Lock()):
        match = await get_match(mid)
        if not match or match.get("stage") != "lobby":
            return
        players_count = len(match.get("players", []))
        if players_count >= config.MIN_PLAYERS:
            try:
                # advance() changes stage before starting the first game, so any
                # duplicate timeout/start callback will observe a non-lobby stage.
                await advance(context, mid)
            except Exception:
                log.exception("Failed to auto-advance game %s", mid)
                latest = await get_match(mid)
                if latest and latest.get("stage") != "finished":
                    latest["stage"] = "finished"
                    await save_match(latest)
                    runtime.clear(mid)
                    await refresh_lobby_scoreboard(context, mid, force=True)
                    try:
                        await safe_edit(
                            context.bot,
                            latest["chat_id"],
                            latest.get("round_message_id") or latest.get("lobby_message_id"),
                            "⚠️ <b>FAILED TO START GAME</b>\n\nThe round could not initialize. Start a new lobby with /enterthehell.",
                            reply_markup=ui.kb([]),
                        )
                    except Exception:
                        log.exception("Failed to display lobby timeout error for match %s", mid)
        else:
            match["stage"] = "finished"
            await save_match(match)
            runtime.clear(mid)
            await safe_edit(
                context.bot,
                match["chat_id"],
                match.get("lobby_message_id"),
                "🛑 <b>LOBBY CLOSED</b>\n\n"
                f"Only {players_count}/{config.MIN_PLAYERS} required players joined.\n"
                "Start a new lobby with /enterthehell when everyone is ready.",
                reply_markup=ui.kb([]),
            )


async def restore_lobby_timers(context):
    """Resume active lobby timers from the same MongoDB collection used by repository."""
    try:
        cursor = matches.find({"stage": "lobby"}).sort("created_at", 1)
        async for match in cursor:
            mid = str(match["_id"])
            if not match.get("lobby_deadline"):
                created_at = match.get("created_at")
                if isinstance(created_at, datetime):
                    if created_at.tzinfo is None:
                        created_at = created_at.replace(tzinfo=timezone.utc)
                    match["lobby_deadline"] = created_at.timestamp() + int(config.LOBBY_SECONDS)
                else:
                    match["lobby_deadline"] = time.time()
                await save_match(match)
            state = runtime.get(mid)
            if any(not task.done() and task.get_name() == f"lobby:{mid}" for task in state.get("tasks", [])):
                continue
            log.info("Restoring lobby timer for match %s", mid)
            task = asyncio.create_task(_lobby_countdown(context, mid), name=f"lobby:{mid}")
            runtime.add_task(mid, task)
    except Exception:
        log.exception("Failed to restore active lobby timers")

async def cancel_game(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    
    if message is None or chat is None or user is None:
        return
        
    if chat.type == "private":
        await context.bot.send_message(chat_id=chat.id, text="👥 Use this command inside a GROUP!")
        return

    # Define Bot Admin ID
    BOT_ADMIN = 6439532660

    match_stub = await get_active_match(chat.id)
    if not match_stub:
        await context.bot.send_message(chat_id=chat.id, text="⚠️ No active game in this group to cancel.")
        return

    mid = str(match_stub["_id"])
    match = await get_match(mid)
    if not match:
        return

    host_id = match.get("host_id") or match.get("created_by")
    
    # Permission Check
    is_authorized = False
    if user.id == BOT_ADMIN:
        is_authorized = True
    elif user.id == host_id:
        is_authorized = True
    else:
        try:
            member = await chat.get_member(user.id)
            if member.status in ("administrator", "creator"):
                is_authorized = True
        except Exception:
            pass

    if not is_authorized:
        await context.bot.send_message(chat_id=chat.id, text="❌ You don't have permission to cancel the game. Only the host, group admins, or bot admin can do this.")
        return

    # Perform Cancellation Safely
    rt = runtime.get(mid)
    async with rt.setdefault("lobby_lock", asyncio.Lock()):
        latest = await get_match(mid)
        if not latest or latest.get("stage") == "finished":
            await context.bot.send_message(chat_id=chat.id, text="⚠️ The game is already finished or doesn't exist.")
            return
        
        latest["stage"] = "finished"
        await save_match(latest)
        runtime.clear(mid)
        
        # Update the active UI card to reflect cancellation
        msg_id = latest.get("round_message_id") or latest.get("lobby_message_id")
        if msg_id:
            try:
                await safe_edit(
                    context.bot,
                    chat.id,
                    msg_id,
                    "🛑 <b>GAME CANCELLED</b>\n\nThis match was forcefully cancelled by an admin.",
                    reply_markup=ui.kb([])
                )
            except Exception:
                pass
        
        await context.bot.send_message(chat_id=chat.id, text="✅ The active game has been successfully cancelled.")


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not message:
        return

    # Optional: If you want only the bot admin to see stats, uncomment the lines below
    # BOT_ADMIN = 6439532660
    # if update.effective_user.id != BOT_ADMIN:
    #     return

    stats = await get_bot_stats()

    text = (
        "📊 <b>Bot Statistics</b>\n\n"
        f"🎮 <b>Total Games:</b> {stats['total_games']}\n"
        f"🔥 <b>Active Games:</b> {stats['active_games']}\n"
        f"👥 <b>Total Users:</b> {stats['total_users']}\n"
        f"🏘 <b>Total Groups:</b> {stats['total_groups']}"
    )

    await message.reply_text(text, parse_mode="HTML")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return

    # User ka ID nikal lein
    user_id = user.id

    text = (
        "🔺 <b>SQUID GAME BOT</b> 🔺\n\n"
        "Welcome to the ultimate survival game! Here is everything you need to know to host, play, and survive.\n\n"
        "🛠 <b>COMMANDS</b>\n"
        "• /enterthehell - Start a new game lobby in a group.\n"
        "• /cancelgame - Forcefully end an active game (Admin/Host only).\n"
        "• /stats - View global bot statistics.\n"
        "• /help - Show this comprehensive guide.\n"
        "• /report - to report a bug and give a suggestion.\n"
    )

    # Button ke data me user_id attach kar diya
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📖 Read Game Guide", callback_data=f"help:guide:{user_id}")]
    ])

    await context.bot.send_message(
        chat_id=message.chat_id,
        text=text,
        parse_mode="HTML",
        reply_markup=keyboard,
        disable_web_page_preview=True
    )


async def help_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the inline buttons for the /help menu with user security."""
    query = update.callback_query
    data = query.data
    parts = data.split(":")

    # Data check (e.g., help:guide:123456789)
    if len(parts) != 3 or parts[0] != "help":
        return
        
    action = parts[1]
    target_user_id = int(parts[2])

    # 🔒 SECURITY CHECK: Sirf wahi click kar payega jisne command di thi
    if query.from_user.id != target_user_id:
        await query.answer("⚠️ Not For You", show_alert=True)
        return

    if action == "guide":
        text = (
            "🦑 <b>THE GAMES (DEEP GUIDE)</b>\n\n"
            "🟢 <b>Round 1: Red Light, Green Light</b>\n"
            "• <b>Objective:</b> Reach the finish line before time runs out.\n"
            "• <b>Rule:</b> Tap <code>⬆️ MOVE FORWARD</code> <i>only</i> when the light is GREEN. If you move on RED, you are eliminated.\n\n"
            "🍪 <b>Round 2: Dalgona Candy</b>\n"
            "• <b>Objective:</b> Memorize and repeat the flashing pattern.\n"
            "• <b>Rule:</b> A shape flashes on the group grid. Repeat the exact 1-9 tile sequence in your DM. One wrong tap breaks your candy.\n\n"
            "🎱 <b>Round 3: Marbles (Odd or Even)</b>\n"
            "• <b>Objective:</b> Steal all opponent's marbles.\n"
            "• <b>Rule:</b> One player hides marbles, the other bets and guesses <b>ODD</b> or <b>EVEN</b>. First to hit 0 marbles dies.\n\n"
            "🌉 <b>Round 4: Glass Bridge</b>\n"
            "• <b>Objective:</b> Cross the bridge safely.\n"
            "• <b>Rule:</b> Choose <b>LEFT</b> or <b>RIGHT</b> glass panels. One is safe, the other shatters.\n\n"
            "🗳 <b>Round 5: Elimination Vote</b>\n"
            "• <b>Objective:</b> Survive the group vote.\n"
            "• <b>Rule:</b> Vote privately in DMs to execute one player. The player with the most votes is eliminated.\n\n"
            "⚔️ <b>Round 6: Squid Game (The Final)</b>\n"
            "• <b>Objective:</b> Win the 1v1 tactical showdown.\n"
            "• <b>Rule:</b> Offensive beats Defensive, Defensive beats Trickster, Trickster beats Offensive."
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Back to Commands", callback_data=f"help:main:{target_user_id}")]
        ])
        
        try:
            await query.answer()
            await query.edit_message_text(text=text, parse_mode="HTML", reply_markup=keyboard)
        except Exception:
            pass

    elif action == "main":
        text = (
            "🔺 <b>SQUID GAME BOT — OFFICIAL GUIDE</b> 🔺\n\n"
            "Welcome to the ultimate survival game! Here is everything you need to know to host, play, and survive.\n\n"
            "🛠 <b>COMMANDS</b>\n"
            "• /enterthehell - Start a new game lobby in a group.\n"
            "• /cancelgame - Forcefully end an active game (Admin/Host only).\n"
            "• /stats - View global bot statistics.\n"
            "• /help - Show this comprehensive guide.\n"
            "• /report - to report a bug and give a suggestion.\n"
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📖 Read Game Guide", callback_data=f"help:guide:{target_user_id}")]
        ])
        
        try:
            await query.answer()
            await query.edit_message_text(text=text, parse_mode="HTML", reply_markup=keyboard)
        except Exception:
            pass



async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if not message or not user or not chat:
        return

    # Telegram uses 'text' for plain messages and 'caption' for media messages
    text_content = message.text or message.caption or ""
    
    args = text_content.split(maxsplit=1)
    
    # Check if they provided text OR attached media
    has_media = bool(message.photo or message.video or message.document or message.animation)
    if len(args) < 2 and not has_media:
        await message.reply_text(
            "⚠️ <b>How to report a bug:</b>\n"
            "<code>/report &lt;describe the issue&gt;</code>\n\n"
            "<i>Tip: You can also attach a photo or video and write /report in the caption!</i>",
            parse_mode="HTML"
        )
        return

    report_text = args[1] if len(args) > 1 else "<i>(Media attached without extra text)</i>"
    BOT_ADMIN = 6439532660  # Your Admin ID

    # Auto-detect location data
    chat_type = chat.type.capitalize()
    chat_name = html.escape(chat.title if chat.title else "Private Message")

    admin_msg = (
        "🚨 <b>NEW BUG REPORT</b> 🚨\n\n"
        f"👤 <b>From:</b> {html.escape(user.full_name)} (<code>{user.id}</code>)\n"
        f"💬 <b>Username:</b> @{user.username if user.username else 'N/A'}\n"
        f"📍 <b>Source:</b> {chat_name} (<code>{chat.id}</code>) — {chat_type}\n\n"
        f"📝 <b>Report Text:</b>\n{html.escape(report_text)}"
    )

    try:
        # 1. Send the detailed text report to Admin
        await context.bot.send_message(
            chat_id=BOT_ADMIN, 
            text=admin_msg, 
            parse_mode="HTML"
        )
        
        # 2. If the user attached media, copy the exact media message to Admin
        # (We use copy_message to bypass users who have forwarding privacy restrictions)
        if has_media:
            await context.bot.copy_message(
                chat_id=BOT_ADMIN,
                from_chat_id=chat.id,
                message_id=message.message_id
            )

        await message.reply_text(
            "✅ <b>Report Submitted!</b>\n\n"
            "Thank you for your feedback. The admin has been notified and will review the issue.",
            parse_mode="HTML"
        )
    except Exception as e:
        log.exception("Failed to send bug report to admin.")
        await message.reply_text(
            "❌ <b>Error:</b> Could not send the report at this time. Please try again later.",
            parse_mode="HTML"
        )