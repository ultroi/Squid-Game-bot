import asyncio
import html
import logging
import re
import time
from urllib.parse import urlsplit
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError, RetryAfter, BadRequest

from . import config

log = logging.getLogger(__name__)

NUM = {1: "1️⃣", 2: "2️⃣", 3: "3️⃣", 4: "4️⃣", 5: "5️⃣",
       6: "6️⃣", 7: "7️⃣", 8: "8️⃣", 9: "9️⃣"}

# RLGL signal artwork for the private rich-message game card.
GREEN_IMG = "https://i.ibb.co/TMkY47Sp/19d2bbda-8ea1-468f-a7d2-a3d805d67b2e.jpg"
RED_IMG = "https://i.ibb.co/gp8gt2s/41768240-4fc8-4ad9-ac41-9aaba8325c8d.jpg"

# Round artwork is sent as a regular Telegram photo above the text announcement.
# ``bridge`` is the existing stage key for the Hopscotch / Glass Bridge round.
ROUND_ANNOUNCEMENT_IMAGES = {
    "rlgl": "https://i.ibb.co/RtTGDPF/0d120550-9d01-4732-a898-db1ca770a047.jpg",
    "dalgona": "https://i.ibb.co/RGqCsDDt/09a98da7-f6ab-4f49-ac64-776eef00371e.jpg",
    "marbles": "https://i.ibb.co/hRzyNb6w/b5b679a0-763d-481e-9262-0d33ef12fbb3.jpg",
    "bridge": "https://i.ibb.co/6czCm9G0/04d482ce-0202-42b0-a1a7-d109c67c6bc6.jpg",
    "vote": "https://i.ibb.co/kskLd5RQ/5bceef12-a1ac-439b-930f-657c3a7a216f.jpg",
    "final": "https://i.ibb.co/C5xQQZby/31f3f3f0-aae7-4c1a-8723-de96c6e53fe0.jpg",
}

def _validate_image_url(image_url):
    if not isinstance(image_url, str) or not image_url.strip():
        raise ValueError("image_url must be a non-empty HTTP(S) URL")
    image_url = image_url.strip()
    parsed = urlsplit(image_url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Image URLs must use HTTP or HTTPS")
    return image_url


def _validate_rich_image_url(image_url):
    return _validate_image_url(image_url)


def _input_rich_message(rich_html, media=None):
    if not isinstance(rich_html, str) or not rich_html:
        raise ValueError("rich_html must be a non-empty string")
    payload = {"html": rich_html}
    if media:
        payload["media"] = list(media)
    return payload


def open_dm_keyboard(bot_username, label="📩 Open Bot DM"):
    username = str(bot_username or "").strip().lstrip("@")
    if not username:
        return kb([])
    return kb([[InlineKeyboardButton(label, url=f"https://t.me/{username}")]])

def btn(text, data):
    return InlineKeyboardButton(text, callback_data=data)


def kb(rows):
    return InlineKeyboardMarkup(rows)


def lobby_keyboard(match_id, can_start, seconds_left=None):
    rows = [
        [
            btn("🙋 Join", f"lob:join:{match_id}"),
            btn("🚪 Leave", f"lob:leave:{match_id}"),
        ]
    ]

    action_row = []
    if can_start:
        action_row.append(btn("▶️ Start Now", f"lob:start:{match_id}"))

    # Strictly < 10 seconds remaining
    if seconds_left is not None and 0 < int(seconds_left) < 10:
        action_row.append(btn("⏱ +10s", f"lob:time:{match_id}"))

    if action_row:
        rows.append(action_row)

    return kb(rows)


ROUND_INFO = {
    "rlgl": (
        1, "RED LIGHT, GREEN LIGHT",
        "Move only while the light is green. Freeze on red; moving on red eliminates you. Reach the finish before time runs out.",
    ),
    "dalgona": (
        2, "DALGONA CANDY",
        "Watch the shared group grid flash its pattern, then repeat it on the numbered 3×3 grid in your private chat. One wrong press breaks your candy.",
    ),
    "marbles": (
        3, "MARBLES — ODD OR EVEN",
        "Play your paired opponent privately. Hide marbles, bet carefully, and win all your opponent's marbles to survive.",
    ),
    "bridge": (
        4, "GLASS BRIDGE",
        "Cross the bridge one step at a time. Choose left or right in your private chat; the wrong glass tile eliminates you.",
    ),
    "vote": (
        5, "ELIMINATION VOTE",
        "Conditional round: when triggered, vote privately for one player. The highest-voted player is eliminated.",
    ),
    "final": (
        6, "SQUID GAME — THE FINAL",
        "Choose your combat move privately. Offensive beats Defensive, Defensive beats Trickster, and Trickster beats Offensive. Last survivor wins.",
    ),
}

ROUND_WAITING_HINTS = {
    "rlgl": "Get ready — your private movement board is opening soon.",
    "dalgona": "Watch the group pattern, then repeat it on your private grid.",
    "marbles": "Get ready to hide marbles, bet, and guess odd or even in your DM.",
    "bridge": "Get ready to choose the safe left or right glass panel in your DM.",
    "vote": "Voting controls will arrive in your private chat when voting opens.",
    "final": "Your duel controls will arrive in your DM when the bracket opens.",
}


def round_waiting_panel(stage, seconds_left, *, total_seconds=30, hint=None):
    total = max(1, int(total_seconds))
    remaining = max(0, min(total, int(seconds_left)))
    completed_blocks = max(0, min(10, round((total - remaining) / total * 10)))
    progress_bar = "▰" * completed_blocks + "▱" * (10 - completed_blocks)
    if remaining > 0:
        countdown = f"⏳ <b>Starting in</b> <code>{remaining:02d}s</code>"
    else:
        countdown = "🚦 <b>STARTING NOW</b>"

    instruction = hint or ROUND_WAITING_HINTS.get(
        str(stage or "").lower(),
    )
    return "\n".join((
        "🎮 <b>ROUND PREPARATION</b>",
        progress_bar,
        "",
        countdown,
        f"<i>{html.escape(str(instruction))}</i>",
    ))


def _match_counts(match, stage=None):
    players = match.get("players") or []
    if stage == "rlgl":
        qualified = sum(1 for player in players if player.get("reached"))
        alive_count = sum(
            1 for player in players
            if player.get("alive", True) and not player.get("reached")
        )
        eliminated = sum(
            1 for player in players
            if not player.get("alive", True) and not player.get("reached")
        )
    else:
        alive_count = sum(1 for player in players if player.get("alive", True))
        qualified = 0
        eliminated = sum(1 for player in players if not player.get("alive", True))
    return alive_count, qualified, eliminated, len(players)

def render_match_scoreboard(match):
    return _match_board_text(match)


async def send_rich_message(bot, chat_id, rich_html, *, media=None, **options):
    data = {
        "chat_id": chat_id,
        "rich_message": _input_rich_message(rich_html, media=media),
    }
    data.update(options)
    return await bot._post("sendRichMessage", data=data)


async def edit_rich_message(
    bot, chat_id, message_id, rich_html, *, media=None, **options
):
    data = {
        "chat_id": chat_id,
        "message_id": message_id,
        "rich_message": _input_rich_message(rich_html, media=media),
    }
    data.update(options)
    return await bot._post("editMessageText", data=data)


def _match_state_line(match):
    """Describe the match's current lifecycle state for the scoreboard."""
    stage_titles = {
        "rlgl": "Red Light, Green Light",
        "dalgona": "Dalgona Candy",
        "marbles": "Marbles — Odd or Even",
        "bridge": "Glass Bridge",
        "vote": "Elimination Vote",
        "final": "Squid Game Final",
    }
    status = match.get("stage")
    current_stage = match.get("current_round_stage")

    if status == "finished" and match.get("champion_id"):
        champion_id = str(match["champion_id"])
        champion = next(
            (
                player for player in (match.get("players") or [])
                if str(player.get("id", player.get("user_id", ""))) == champion_id
            ),
            {},
        )
        return "🏆 CHAMPION · " + html.escape(str(champion.get("name") or champion_id))
    if status == "finished":
        return "🏁 GAMES COMPLETE"
    if status == "intermission":
        gate = match.get("next_round") or {}
        next_titles = {
            "rlgl": "Dalgona Candy",
            "dalgona": "Marbles",
            "marbles": "Glass Bridge",
            "bridge": "Vote / Final",
            "vote": "Squid Game Final",
            "final": "Final Bracket",
        }
        if gate.get("status") == "open":
            title = next_titles.get(gate.get("from_stage"), "Next Round")
            return "⏳ NEXT-ROUND ENTRY OPEN · UP NEXT: " + html.escape(title)
        return "⏳ ROUND TRANSITION"
    if match.get("round_preparing"):
        return "⏳ PREPARING · " + html.escape(stage_titles.get(current_stage, "Next Round"))
    if current_stage:
        return "🎮 IN PROGRESS · " + html.escape(
            stage_titles.get(current_stage, current_stage.upper())
        )
    return "🎮 MATCH IN PROGRESS"


def _player_round_cell(player, player_id, stage, current_stage, participants, results, preparing, game_state):
    """Render one player's status cell for a single game stage."""
    result = results.get(stage) or {}
    qualified = {str(value) for value in result.get("qualified_ids", [])}
    eliminated = {str(value) for value in result.get("eliminated_ids", [])}

    if player_id in qualified:
        return "✅"
    if player_id in eliminated:
        return "💀"
    if stage != current_stage or (participants and player_id not in participants):
        return "—"
    if preparing:
        return "⏳"
    if not player.get("alive", True):
        return "💀"
    if stage == "rlgl" and player.get("reached"):
        return "✅"
    if stage == "dalgona" and game_state.get("done"):
        return "✅"
    return "🎮" if player.get("alive", True) else "—"


def _match_player_row(index, player, columns, current_stage, participants, results, preparing, game_states):
    player_id = str(player.get("id", player.get("user_id", "")))
    name = str(
        player.get("name") or player.get("display_name") or player_id or f"Player {index}"
    ).replace("\n", " ").replace("\r", " ")
    name = html.escape(name[:40])
    game_state = game_states.get(player_id) or {}
    cells = [
        _player_round_cell(
            player, player_id, stage, current_stage, participants,
            results, preparing, game_state,
        )
        for stage in columns
    ]
    return (
        "<tr>"
        f'<td align="center">{index:02d}</td>'
        f"<td>{name}</td>"
        + "".join(f'<td align="center">{cell}</td>' for cell in cells)
        + "</tr>"
    )


def _match_board_text(match):
    players = match.get("players") or []
    results = match.get("round_results") or {}
    current_stage = match.get("current_round_stage")
    participants = {str(uid) for uid in match.get("current_round_player_ids", [])}
    game_states = (match.get("game") or {}).get("players") or {}
    columns = ("rlgl", "dalgona", "marbles", "bridge", "vote", "final")
    labels = ("RLGL", "DALG", "MARB", "BRDG", "VOTE", "FINL")

    total = len(players)
    alive_count = sum(1 for player in players if player.get("alive", True))
    eliminated_count = total - alive_count
    lines = [
        "<h2>🔺 SQUID GAME · MATCH BOARD</h2>",
        f"<p>{_match_state_line(match)}</p>",
        f"<p>👥 Total: <b>{total}</b> · 🟢 Alive: <b>{alive_count}</b> · 💀 Eliminated: <b>{eliminated_count}</b></p>",
        "<h3>📊 PLAYER ROUND RESULTS</h3>",
        '<table bordered striped compact>',
        '<tr><th align="center">NO</th><th>PLAYER</th>'
        + "".join(f'<th align="center">{label}</th>' for label in labels)
        + "</tr>",
    ]

    for index, player in enumerate(players, 1):
        lines.append(
            _match_player_row(
                index, player, columns, current_stage, participants, results,
                bool(match.get("round_preparing")), game_states,
            )
        )
    if not players:
        lines.append('<tr><td colspan="8">No registered players yet.</td></tr>')
    lines.extend(["</table>"])
    return "\n".join(lines)


def render_lobby(match, seconds_left=None, min_players=None, max_players=None):
    if match.get("current_round_stage") or match.get("round_results"):
        return _match_board_text(match)

    players = match.get("players") or []
    minimum = max(1, int(min_players if min_players is not None else getattr(config, "MIN_PLAYERS", 5)))
    maximum_value = max_players if max_players is not None else match.get("max_players")
    if maximum_value is None:
        maximum_value = getattr(config, "MAX_PLAYERS", 20)
    maximum = max(1, int(maximum_value))

    host_id = match.get("created_by") or match.get("host_id")

    def player_id(player):
        return player.get("id") if player.get("id") is not None else player.get("user_id")

    host = next((p for p in players if str(player_id(p)) == str(host_id)), None) if host_id is not None else None
    host_name = (host or {}).get("name") or (host or {}).get("display_name")
    host_label = html.escape(str(host_name)[:32]) if host_name else ("Host" if host_id is not None else "Not assigned")
    if host_id is not None:
        host_html = f'<a href="tg://user?id={html.escape(str(host_id), quote=True)}">{host_label}</a>'
    else:
        host_html = host_label

    count = len(players)
    remain = max(0, int(seconds_left)) if seconds_left is not None else None
    readiness = (
        "✅ <b>Lobby ready</b> · Host can start"
        if count >= minimum
        else f"⏳ <b>{minimum - count} more player{'s' if minimum - count != 1 else ''} needed</b>"
    )
    lines = [
        "🔺 <b>SQUID GAME</b> · <b>LOBBY</b>",
        "",
        f"👥 <b>{count}/{maximum}</b> players  ·  👑 {host_html}",
        (f"⏱ Auto-start in <b>{remain}s</b>" if remain is not None else "Join the lobby to enter the games."),
        readiness,
        "",
        "<b>THE GAMES</b>",
        "🚦 Red Light  ·  🍪 Dalgona  ·  🎱 Marbles",
        "🌉 Glass Bridge  ·  🗳 Vote*  ·  ⚔️ Final",
        "",
        f"<b>PLAYERS</b> · {count}",
        "",
    ]
    if not players:
        lines.append("<i>No players yet — tap Join to enter.</i>")
    else:
        visible = players[:10]
        for index, player in enumerate(visible, 1):
            uid = player_id(player)
            name = html.escape(str(player.get("name") or player.get("display_name") or uid or f"Player {index}")[:26])
            emoji = html.escape(str(player.get("emoji") or "👤"))
            if uid is not None:
                name = f'<a href="tg://user?id={html.escape(str(uid), quote=True)}">{name}</a>'
            lines.append(f"{index:02d}. {emoji} {name}")
        if count > len(visible):
            lines.append(f"<i>…and {count - len(visible)} more players</i>")
    lines.append("<i>Use Join below. Your private game controls arrive when each round begins.</i>")
    return "\n".join(lines)




def round_result_text(match, stage, event=None):
    """Sabhi rounds ke liye proper formatted standalone result message."""
    round_number, title, _ = ROUND_INFO.get(
        stage, (0, str(stage).replace("_", " ").upper(), "")
    )
    players = match.get("players") or []
    
    # Current round ke participants filter karein taaki pichle round ke dead players mix na hon
    participants = {str(uid) for uid in (match.get("current_round_player_ids") or [])}
    if not participants:
        game_players = (match.get("game") or {}).get("players")
        if isinstance(game_players, dict) and game_players:
            participants = {str(uid) for uid in game_players.keys()}

    def in_round(p):
        return not participants or str(p.get("id")) in participants

    def format_player_link(p):
        """Helper to format player name as a Telegram profile hyperlink."""
        uid = p.get("id") or p.get("user_id")
        name = html.escape(str(p.get("name") or p.get("display_name") or uid))
        if uid:
            return f'<a href="tg://user?id={uid}">{name}</a>'
        return name

    if stage == "rlgl":
        qualified = [
            format_player_link(p)
            for p in players if in_round(p) and p.get("reached")
        ]
        eliminated = [
            format_player_link(p)
            for p in players if in_round(p) and not p.get("alive") and not p.get("reached")
        ]
    else:
        qualified = [
            format_player_link(p)
            for p in players if in_round(p) and p.get("alive")
        ]
        eliminated = [
            format_player_link(p)
            for p in players if in_round(p) and not p.get("alive")
        ]

    qualified_text = ", ".join(qualified) if qualified else "None"
    eliminated_text = ", ".join(eliminated) if eliminated else "None"

    heading = (
        f"🏁 <b>ROUND {round_number} RESULT</b>"
        if round_number else f"🏁 <b>{html.escape(title)} — RESULTS</b>"
    )

    lines = [
        "━━━━━━━━━━━━━━━━━━━━",
        heading,
        "━━━━━━━━━━━━━━━━━━━━",
        "",
        f"✅ <b>Qualified ({len(qualified)}):</b>\n{qualified_text}",
        f"💀 <b>Eliminated ({len(eliminated)}):</b>\n{eliminated_text}",
    ]

    if event:
        lines.extend(["", f"📣 <i>{str(event)}</i>"])

    if not qualified:
        lines.extend(["", "❌ <b>No one qualified.</b> The match is over."])

    return "\n".join(lines)


def round_status_text(match, stage, details=None, event=None, finished=False):
    round_number, title, description = ROUND_INFO.get(
        stage, (0, str(stage).replace("_", " ").upper(), "Follow the instructions below.")
    )
    alive_count, qualified, eliminated, total = _match_counts(match, stage)
    
    # 1. Clean Header
    heading = (
        f"🦑 <b>ROUND {round_number} · {html.escape(title)}</b>"
        if round_number else f"🦑 <b>{html.escape(title)}</b>"
    )

    # 2. Highlighted Status Line
    if finished:
        status_line = "🏁 <b>STATUS:</b> <code>ROUND COMPLETE</code>"
    elif match.get("round_preparing"):
        status_line = "⏳ <b>STATUS:</b> <code>PREPARING...</code>"
    else:
        status_line = "🟢 <b>STATUS:</b> <code>IN PROGRESS</code>"

    # 3. Better Spaced Stats
    if stage == "rlgl":
        stats_line = f"🏃 <b>Racing:</b> {alive_count}  |  🏁 <b>Finished:</b> {qualified}  |  💀 <b>Out:</b> {eliminated}"
    else:
        stats_line = f"👥 <b>Alive:</b> {alive_count}  |  💀 <b>Out:</b> {eliminated}  |  📊 <b>Total:</b> {total}"

    # 4. Structured Additional Blocks (Result block yahan se hata diya gaya hai taaki announcement me result add na ho)
    details_block = f"\n\n{details}" if details else ""

    return (
        f"{heading}\n"
        f"<i>{html.escape(description)}</i>\n\n"
        f"{status_line}\n"
        f"{stats_line}"
        f"{details_block}"
    )


def next_round_keyboard(match_id):
    return kb([[btn("✅ Enter Next Round", f"nr:enter:{match_id}")]])


def next_round_gate_text(match, seconds_left=None):
    gate = match.get("next_round") or {}
    eligible = gate.get("eligible_ids") or []
    joined = gate.get("joined_ids") or []
    if seconds_left is None:
        seconds_left = max(0, int(float(gate.get("deadline", time.time())) - time.time() + 0.999))

    source_title = html.escape(str(gate.get("completed_title") or "Previous round"))
    from_stage = gate.get("from_stage")
    next_copy = {
        "rlgl": ("🍪 <b>NEXT · DALGONA CANDY</b>", "Watch the shared group pattern, then repeat it on the private numbered grid. One wrong tap or timeout eliminates you."),
        "dalgona": ("🎱 <b>NEXT · MARBLES</b>", "Play your paired opponent privately. Hide marbles, bet carefully and win to survive."),
        "marbles": ("🌉 <b>NEXT · GLASS BRIDGE</b>", "Choose left or right privately. A wrong glass tile eliminates you."),
        "bridge": ("🗳 <b> NEXT · VOTE OR FINAL</b>", "The next round depends on how many qualified players enter."),
        "vote": ("⚔️ <b>NEXT · SQUID GAME FINAL</b>", "Choose tactical combat moves. One champion remains."),
        "final": ("⚔️ <b>NEXT · FINAL BRACKET</b>", "Enter to continue into the next set of tactical duels."),
    }
    next_heading, next_description = next_copy.get(
        from_stage, ("🎮 <b>UP NEXT</b>", "Enter to continue the competition.")
    )
    
    if gate.get("status") != "open":
        action_line = "⏳ <b>Entry closed</b>"
    else:
        action_line = f"⏳ <b>{seconds_left}s left to enter</b>"

    # --- EASY TO EDIT UI FORMAT ---
    return (
        f"🏁 <b>{source_title}</b> complete!\n\n"
        f"{next_heading}\n"
        f"<i>{next_description}</i>\n\n"
        f"🎟 <b>Entry:</b> {len(joined)}/{len(eligible)} qualified players\n"
        f"{action_line}\n"
        f"<i>Tap below to continue. Missing the timer means elimination.</i>"
    )

def round_preparation_text(stage, entrants_count, seconds_left):
    round_number, title, description = ROUND_INFO.get(
        stage, (0, str(stage).replace("_", " ").upper(), "Follow the round instructions.")
    )
    heading = f"ROUND {round_number}/6" if round_number else "NEXT ROUND"
    
    # --- EASY TO EDIT UI FORMAT ---
    return (
        f"🔺 <b>SQUID GAME · {heading}</b>\n"
        f"<b>{html.escape(title)}</b>\n"
        f"<i>{html.escape(description)}</i>\n"
        f"\n"
        f"👥 Entered players: <b>{entrants_count}</b>\n"
        f"\n\n"
        f"⏳ <b>Starting in {seconds_left}s…</b>"
    )

async def _send_round_message(
    bot, chat_id, stage, text, reply_markup=None, *, include_banner=False
):
    """Send a Telegram message for a round announcement with an attached photo banner and caption."""
    image_url = ROUND_ANNOUNCEMENT_IMAGES.get(str(stage or "").lower()) if include_banner else None
    
    if image_url:
        try:
            return await bot.send_photo(
                chat_id=chat_id,
                photo=_validate_image_url(image_url),
                caption=text,
                parse_mode="HTML",
                reply_markup=reply_markup,
            )
        except TelegramError as exc:
            log.warning("Round announcement photo banner failed for %s, falling back to text: %s", stage, exc)

    return await bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode="HTML",
        reply_markup=reply_markup,
        disable_web_page_preview=True,
    )

async def _edit_round_message(bot, chat_id, message_id, stage, text, reply_markup=None):
    """Update a round announcement. Safely updates caption if it's a photo message, otherwise updates text."""
    try:
        # 1. Try editing the photo caption first
        return await bot.edit_message_caption(
            chat_id=chat_id,
            message_id=message_id,
            caption=text,
            parse_mode="HTML",
            reply_markup=reply_markup,
        )
    except BadRequest as exc:
        err_msg = str(exc).lower()
        # If the caption is identical, do nothing and return True!
        if "message is not modified" in err_msg:
            return True
        
        # Only fall back to edit_message_text if the message is actually a text message (not a photo)
        if "no caption in the message to edit" in err_msg or "message_id_invalid" not in err_msg:
            try:
                return await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=text,
                    parse_mode="HTML",
                    reply_markup=reply_markup,
                    disable_web_page_preview=True,
                )
            except BadRequest as text_exc:
                if "message is not modified" in str(text_exc).lower():
                    return True
                raise
        raise

async def send_round_announcement(bot, match, stage, details=None, event=None, reply_markup=None):
    """Post a normal editable announcement, preceded by a regular photo banner."""
    text = round_status_text(match, stage, details=details, event=event)
    message = None
    for attempt in range(2):
        try:
            message = await _send_round_message(
                bot,
                match["chat_id"],
                stage,
                text,
                reply_markup=reply_markup,
                include_banner=(attempt == 0),
            )
            break
        except RetryAfter as exc:
            if attempt == 0:
                delay = getattr(exc, "retry_after", 1)
                await asyncio.sleep(float(getattr(delay, "total_seconds", lambda: delay)()) + 0.2)
            else:
                raise
        except TelegramError:
            if attempt:
                raise
            await asyncio.sleep(0.2)

    if message is None:
        raise RuntimeError("Telegram did not return a round announcement message")

    match["round_message_id"] = message.message_id
    match["round_message_stage"] = stage
    # A new round resets the marker for the last round's separate result message.
    match["round_result_message_id"] = None
    match["round_result_message_stage"] = None
    from .repository import save_match
    await save_match(match)
    return message


async def send_round_result_message(bot, match, stage=None, event=None):
    """Har round ka result ek naye standalone message me GC ke andar bhejega."""
    stage = stage or match.get("round_message_stage") or match.get("stage", "unknown")
    
    # Duplicate result message jane se rokein
    if (
        match.get("round_result_message_id")
        and match.get("round_result_message_stage") == stage
    ):
        return True

    text = round_result_text(match, stage, event=event)
    for attempt in range(3):
        try:
            message = await bot.send_message(
                chat_id=match["chat_id"],
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            match["round_result_message_id"] = message.message_id
            match["round_result_message_stage"] = stage
            from .repository import save_match
            await save_match(match)
            return True
        except RetryAfter as exc:
            delay = getattr(exc, "retry_after", 1)
            if hasattr(delay, "total_seconds"):
                delay = delay.total_seconds()
            if attempt < 2:
                await asyncio.sleep(max(0.0, float(delay)) + 0.2)
                continue
        except TelegramError:
            log.exception("Could not send round result message for %s", stage)
            break
    return False


async def refresh_round_announcement(bot, match, stage=None, details=None, event=None, finished=False, reply_markup=None):
    stage = stage or match.get("round_message_stage") or match.get("stage", "unknown")
    
    # 1. Generate text (yeh automatically finished=True hone par 'ROUND COMPLETE' likh dega)
    text = round_status_text(match, stage, details=details, event=event, finished=finished)
    message_id = match.get("round_message_id")
    
    current_markup = kb([]) if finished else reply_markup

    # 2. Purane Round Announcement Card ko Edit karein
    if not message_id:
        if not finished:  # Agar message nahi hai aur round chal raha hai tabhi naya announcement bhejein
            message = await _send_round_message(
                bot, match["chat_id"], stage, text, reply_markup=current_markup
            )
            match["round_message_id"] = message.message_id
            match["round_message_stage"] = stage
            from .repository import save_match
            await save_match(match)
    else:
        for attempt in range(2):
            try:
                await _edit_round_message(
                    bot,
                    match["chat_id"],
                    message_id,
                    stage,
                    text,
                    reply_markup=current_markup,
                )
                break
            except BadRequest as exc:
                if "message is not modified" in str(exc).lower():
                    break
                log.warning("Round announcement edit failed for %s: %s", stage, exc)
                break
            except RetryAfter as exc:
                if attempt == 0:
                    delay = getattr(exc, "retry_after", 1)
                    if hasattr(delay, "total_seconds"):
                        delay = delay.total_seconds()
                    await asyncio.sleep(float(delay) + 0.2)
                else:
                    break
            except TelegramError:
                log.exception("Round announcement edit failed for %s", stage)
                break

    # 3. Purana card update hone ke baad, NAYA Standalone Result message GC me bhejein
    if finished:
        return await send_round_result_message(bot, match, stage=stage, event=event)

    return True


# ---------- red light green light ----------


def _player_position(player, size):
    pos = player.get("pos") or [size - 1, size // 2]
    try:
        row, col = int(pos[0]), int(pos[1])
    except (TypeError, ValueError, IndexError, KeyError):
        return size - 1, size // 2
    if not (0 <= row < size and 0 <= col < size):
        return size - 1, size // 2
    return row, col
 
 
def _rlgl_counts(players):
    alive_count = sum(
        1 for player in players
        if player.get("alive", True) and not player.get("reached")
    )
    finished_count = sum(1 for player in players if player.get("reached"))
    out_count = sum(
        1 for player in players
        if not player.get("alive", True) and not player.get("reached")
    )
    return alive_count, finished_count, out_count
 
 
def _rlgl_progress(player, size):
    """Steps completed towards the finish (0 .. size-1)."""
    if player.get("reached"):
        return size - 1
    row, _ = _player_position(player, size)
    return (size - 1) - row
 
 
def rlgl_render_context(match, light=None):
    """Everything that is identical for every viewer, computed once per render batch."""
    players = match.get("players") or []
    size = max(1, int(config.GRID_SIZE))
 
    occupants = {}
    for player in players:
        row, col = _player_position(player, size)
        dead = not player.get("alive", True) and not player.get("reached")
        emoji = "❌" if dead else html.escape(str(player.get("emoji") or "👤"))
        occupants.setdefault((row, col), []).append(
            (str(player.get("id")), emoji, dead)
        )
 
    ranked = sorted(
        players,
        key=lambda p: (
            1 if p.get("reached") else 0,
            1 if p.get("alive", True) else 0,
            _rlgl_progress(p, size),
        ),
        reverse=True,
    )
    rank = {str(p.get("id")): index for index, p in enumerate(ranked, 1)}
 
    return {
        "size": size,
        "occupants": occupants,
        "ranked": ranked,
        "rank": rank,
        "counts": _rlgl_counts(players),
        "total": len(players),
    }
 
 
def _rlgl_table(ctx, viewer_id=None):
    """Race grid. The viewer's own cell is shown as 📍 so they can always find themselves."""
    size = ctx["size"]
    occupants = ctx["occupants"]
    viewer = str(viewer_id) if viewer_id is not None else None
 
    rows = ['<table bordered striped compact>']
    for row in range(size):
        cells = []
        for col in range(size):
            at_cell = occupants.get((row, col), [])
            if at_cell:
                # Put the viewer first so the marker is never buried in a crowded cell.
                ordered = sorted(at_cell, key=lambda item: item[0] != viewer)
                value = "".join(
                    "📍" if (uid == viewer and not dead) else emoji
                    for uid, emoji, dead in ordered
                )
            elif row == 0:
                value = "🏁"
            elif row == size - 1:
                value = "─"
            else:
                value = "·"
            cells.append(f'<td align="center" valign="middle">{value}</td>')
        rows.append("<tr>" + "".join(cells) + "</tr>")
    rows.append("</table>")
    return "".join(rows)
 
 
def _rlgl_standings(ctx, viewer_id=None, limit=5):
    """Top racers by progress, plus the viewer's own row when they are outside the top."""
    size = ctx["size"]
    ranked = ctx["ranked"]
    viewer = str(viewer_id) if viewer_id is not None else None
 
    rows = [
        '<table bordered striped compact>',
        '<caption>🏁 TOP RACERS</caption>',
    ]
    if not ranked:
        rows.extend(['<tr><td><i>Waiting for players…</i></td></tr>', '</table>'])
        return "".join(rows)
 
    def row_html(rank, player):
        name = html.escape(str(player.get("name") or f"Player {rank}")[:14])
        emoji = html.escape(str(player.get("emoji") or "👤"))
        if viewer is not None and str(player.get("id")) == viewer:
            name = f"<b>{name}</b>"
        if player.get("reached"):
            status = "🏁 FINISHED"
        elif player.get("alive", True):
            status = f"🟢 {_rlgl_progress(player, size)}/{size - 1}"
        else:
            status = "💀 OUT"
        return (
            f'<tr><td align="center">{rank}</td>'
            f"<td>{emoji} {name}</td><td>{status}</td></tr>"
        )
 
    for rank, player in enumerate(ranked[:limit], 1):
        rows.append(row_html(rank, player))
 
    if viewer is not None:
        viewer_rank = ctx["rank"].get(viewer)
        if viewer_rank and viewer_rank > limit:
            rows.append('<tr><td align="center">…</td><td>…</td><td>…</td></tr>')
            rows.append(row_html(viewer_rank, ranked[viewer_rank - 1]))
 
    rows.append("</table>")
    return "".join(rows)
 
 
def _rlgl_viewer_line(ctx, viewer):
    if not viewer:
        return ""
    size = ctx["size"]
    rank = ctx["rank"].get(str(viewer.get("id")))
    total = ctx["total"]
    if viewer.get("reached"):
        return f"<p>🏁 <b>You finished</b> · rank {rank}/{total}</p>"
    if not viewer.get("alive", True):
        return ""
    row, _ = _player_position(viewer, size)
    return (
        f"<p>📍 <b>You:</b> {row} step{'s' if row != 1 else ''} to the finish"
        f" · rank {rank}/{total}</p>"
    )
def rlgl_image_url(light):
    """Return the original RLGL artwork URL without proxying or transforming it."""
    source = GREEN_IMG if str(light or "green").lower() == "green" else RED_IMG
    return _validate_rich_image_url(source)
 
 
def _rlgl_signal_text(light, round_finished, seconds_left):
    green = str(light or "green").lower() == "green"
    if round_finished:
        return (
            "🏁 ROUND FINISHED",
            "This round has ended. Final positions and standings are shown below.",
            '<p>⏹ <b>Round status:</b> <code>FINISHED</code></p>',
        )

    signal = "🟢 GREEN LIGHT — MOVE" if green else "🔴 RED LIGHT — FREEZE"
    instruction = (
        "The doll is turned away. Move toward the finish."
        if green else "The doll is watching. Freeze now; moving will eliminate you."
    )
    timer = (
        f'<p>⏱ <b>Time left:</b> <code>~{max(0, int(seconds_left))}s</code></p>'
        if seconds_left is not None else ""
    )
    return signal, instruction, timer


def _rlgl_terminal_message(viewer):
    if viewer and viewer.get("reached"):
        return (
            '<blockquote>🏁 <b>YOU HAVE QUALIFIED</b>\n'
            "You reached the finish. Wait for the next round.</blockquote>"
        )
    if viewer and not viewer.get("alive", True):
        return (
            '<blockquote>❌ <b>YOU ARE ELIMINATED</b>\n'
            "Movement controls are disabled.</blockquote>"
        )
    return ""


def _rlgl_controls(match_id, phase_token, round_finished, viewer, green):
    if match_id is None:
        return ""

    safe_match_id = html.escape(str(match_id), quote=True)
    safe_phase = html.escape(str(phase_token if phase_token is not None else "0"), quote=True)
    if round_finished:
        style, data, label = "primary", "finished", "🏁 ROUND FINISHED · WAIT"
        return (
            '<tg-button-row align="center">'
            f'<tg-button type="callback_data" style="{style}" '
            f'data="rlgl:{safe_match_id}:{safe_phase}:{data}">{label}</tg-button>'
            '</tg-button-row>'
        )
    if viewer and viewer.get("reached"):
        return (
            '<tg-button-row align="center">'
            f'<tg-button type="callback_data" style="success" '
            f'data="rlgl:{safe_match_id}:{safe_phase}:finished">🏁 QUALIFIED · WAIT</tg-button>'
            '</tg-button-row>'
        )
    if viewer and not viewer.get("alive", True):
        return (
            '<tg-button-row align="center">'
            f'<tg-button type="callback_data" style="danger" '
            f'data="rlgl:{safe_match_id}:{safe_phase}:dead">❌ ELIMINATED · NO CONTROLS</tg-button>'
            '</tg-button-row>'
        )

    forward_style = "success" if green else "danger"
    return (
        '<tg-button-row align="center">'
        f'<tg-button type="callback_data" style="{forward_style}" '
        f'data="rlgl:{safe_match_id}:{safe_phase}:up">⬆️ MOVE FORWARD</tg-button>'
        '</tg-button-row>'
    )


def _rlgl_selected_image(light, image_url):
    try:
        return _validate_rich_image_url(image_url) if image_url else rlgl_image_url(light)
    except ValueError:
        return rlgl_image_url(light)


def rlgl_dm_html(
    match,
    viewer_id,
    light,
    seconds_left=None,
    match_id=None,
    round_finished=False,
    phase_token=None,
    image_url=None,
    ctx=None,
):
    """Render the private RLGL card from match state and a shared render context."""
    players = match.get("players") or []
    if ctx is None:
        ctx = rlgl_render_context(match, light)

    game_data = match.get("game") or {}
    round_finished = bool(round_finished or game_data.get("round_finished"))
    green = str(light or "green").lower() == "green"
    signal, instruction, timer_line = _rlgl_signal_text(light, round_finished, seconds_left)

    viewer = next(
        (player for player in players if str(player.get("id")) == str(viewer_id)),
        None,
    )
    terminal = _rlgl_terminal_message(viewer)
    alive_count, finished_count, out_count = ctx["counts"]
    controls = _rlgl_controls(
        match_id, phase_token, round_finished, viewer, green,
    )
    chosen_image_url = _rlgl_selected_image(light, image_url)

    parts = [
        f'<img src="{html.escape(chosen_image_url, quote=True)}"/>',
        '<blockquote>'
        f'<h2>{signal}</h2>'
        f'<i>{instruction}</i>'
        '</blockquote>',
        timer_line,
        terminal,
        _rlgl_viewer_line(ctx, viewer),
        '<p>'
        f'👥 <b>Racing:</b> {alive_count} &nbsp; '
        f'🏁 <b>Finished:</b> {finished_count} &nbsp; '
        f'❌ <b>Out:</b> {out_count}'
        '</p>',
        _rlgl_table(ctx, viewer_id),
        '<p><i>📍 you · 🏁 finish line · ❌ eliminated</i></p>',
        _rlgl_standings(ctx, viewer_id),
        controls,
    ]
    return "".join(part for part in parts if part)

 
 
async def send_rlgl_rich_message(bot, chat_id, rich_html, *, media=None):
    return await send_rich_message(bot, chat_id, rich_html, media=media)
 
 
async def edit_rlgl_rich_message(bot, chat_id, message_id, rich_html, *, media=None):
    return await edit_rich_message(
        bot, chat_id, message_id, rich_html, media=media
    )

# ---------- dalgona ----------

def dalgona_display_keyboard(match_id, round_no, active_cell=None):
    rows = []
    for row_index in range(3):
        row = []
        for col_index in range(3):
            cell = row_index * 3 + col_index + 1
            label = "🟨" if cell == active_cell else "▫️"
            row.append(btn(label, f"dal:{match_id}:{round_no}:{cell}"))
        rows.append(row)
    return kb(rows)


def dalgona_keyboard(match_id, round_no=1):
    return kb([[btn(NUM[r * 3 + c + 1], f"dal:{match_id}:{round_no}:{r * 3 + c + 1}")
                for c in range(3)] for r in range(3)])


# ---------- marbles ----------

def marbles_count_keyboard(match_id, action, max_n):
    rows, row = [], []
    for n in range(1, max_n + 1):
        row.append(btn(f"{n}🎱", f"mar:{action}:{match_id}:{n}"))
        if len(row) == 5:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return kb(rows)


def oddeven_keyboard(match_id):
    return kb([[btn("ODD 🎲", f"mar:guess:{match_id}:odd"),
                btn("EVEN 🎲", f"mar:guess:{match_id}:even")]])


# ---------- glass bridge ----------

def bridge_keyboard(match_id):
    return kb([[btn("⬅️ LEFT GLASS", f"bri:{match_id}:0"),
                btn("RIGHT GLASS ➡️", f"bri:{match_id}:1")]])


def bridge_grid(game):
    broken = {tuple(x) for x in game["broken"]}
    progress, tiles = game["progress"], game["tiles"]
    lines = []
    for row in range(config.BRIDGE_LENGTH - 1, -1, -1):
        cells = []
        for side in (0, 1):
            if (row, side) in broken:
                cells.append("💥")
            elif row <= progress and side == tiles[row]:
                cells.append("✅")
            else:
                cells.append("🟦")
        marker = " 👈 next step" if row == progress + 1 else ""
        lines.append("".join(cells) + marker)
    return "\n".join(lines)


# ---------- voting ----------

def vote_keyboard(match, match_id, voter_id):
    return kb([[btn(f"{p['emoji']} {p['name']}", f"vot:{match_id}:{p['id']}")]
               for p in match["players"] if p["alive"] and p["id"] != voter_id])


# ---------- squid game final ----------

def final_keyboard(match_id):
    return kb([
        [btn("⚔️ OFFENSIVE", f"fin:{match_id}:offense")],
        [btn("🛡️ DEFENSIVE", f"fin:{match_id}:defense")],
        [btn("🃏 TRICKSTER", f"fin:{match_id}:trickster")],
    ])


def hp_bar(hp):
    filled = max(0, min(10, round(hp / 10)))
    return "🟥" * filled + "⬛" * (10 - filled)