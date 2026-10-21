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

# Round artwork is embedded into the announcement's Rich Message HTML.
# ``bridge`` is the existing stage key for the Hopscotch / Glass Bridge round.
ROUND_ANNOUNCEMENT_IMAGES = {
    "rlgl": "https://i.ibb.co/jvqp1055/6c583f6f-06f9-4f09-a54b-500de3ade3d9.jpg",
    "dalgona": "https://i.ibb.co/0V6Q5xHg/917ed946-9715-49be-8b3d-ac755502af98.jpg",
    "marbles": "https://i.ibb.co/4Zd5gkYn/1279a769-bd2d-41df-a142-6dcdb1eb4fe0.jpg",
    "bridge": "https://i.ibb.co/DHYGK6jM/6e1fc28c-b315-409b-8293-d65459a0884f.jpg",
    "vote": "https://i.ibb.co/gMn4YpHL/5ff81dd3-84c9-4cfb-9390-3ac406e2b6af.jpg",
    "final": "https://i.ibb.co/nsj9FyCj/c129d016-baa2-400c-91ea-0f5102289e0b.jpg",
}



def _validate_rich_image_url(image_url):
    if not isinstance(image_url, str) or not image_url.strip():
        raise ValueError("image_url must be a non-empty HTTP(S) URL")
    image_url = image_url.strip()
    parsed = urlsplit(image_url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Rich HTML media URLs must use HTTP or HTTPS")
    return image_url



def _input_rich_message(rich_html, media=None):
    if not isinstance(rich_html, str) or not rich_html:
        raise ValueError("rich_html must be a non-empty string")
    payload = {"html": rich_html}
    if media:
        payload["media"] = list(media)
    return payload


def btn(text, data):
    return InlineKeyboardButton(text, callback_data=data)


def kb(rows):
    return InlineKeyboardMarkup(rows)


def lobby_keyboard(match_id, can_start):
    rows = [[btn("🙋 Join", f"lob:join:{match_id}")]]
    if can_start:
        rows.append([btn("▶️ Start Now", f"lob:start:{match_id}")])
    return kb(rows)


def open_dm_keyboard(bot_username, label="📩 Open Bot DM"):
    username = str(bot_username or "").strip().lstrip("@")
    if not username:
        return kb([])
    return kb([[InlineKeyboardButton(label, url=f"https://t.me/{username}")]])


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


def _round_result_block(match, stage):
    """Build the qualified/eliminated summary shown when a round ends."""
    players = match.get("players") or []
    if stage == "rlgl":
        qualified = [
            html.escape(str(player.get("name") or player.get("id")))
            for player in players if player.get("reached")
        ]
        eliminated = [
            html.escape(str(player.get("name") or player.get("id")))
            for player in players
            if not player.get("alive") and not player.get("reached")
        ]
    else:
        qualified = [
            html.escape(str(player.get("name") or player.get("id")))
            for player in players if player.get("alive")
        ]
        eliminated = [
            html.escape(str(player.get("name") or player.get("id")))
            for player in players if not player.get("alive")
        ]

    qualified_text = ", ".join(qualified) if qualified else "None"
    eliminated_text = ", ".join(eliminated) if eliminated else "None"
    return (
        f"\n\n📊 <b>RESULTS</b>\n"
        f"✅ <b>Qualified ({len(qualified)}):</b> {qualified_text}\n"
        f"💀 <b>Eliminated ({len(eliminated)}):</b> {eliminated_text}"
    )


def round_status_text(match, stage, details=None, event=None, finished=False):
    round_number, title, description = ROUND_INFO.get(
        stage, (0, str(stage).replace("_", " ").upper(), "Follow the instructions below.")
    )
    alive_count, qualified, eliminated, total = _match_counts(match, stage)
    heading = (
        f"🦑 <b>SQUID GAME · ROUND {round_number}</b>"
        if round_number else "🦑 <b>SQUID GAME</b>"
    )

    if finished:
        status_line = "🏁 <b>ROUND COMPLETE</b>"
    elif match.get("round_preparing"):
        status_line = "⏳ <b>PREPARING · CONTROLS OPEN SOON</b>"
    else:
        status_line = "🟢 <b>IN PROGRESS</b>"

    if stage == "rlgl":
        stats_line = f"🏃 {alive_count} racing  ·  🏁 {qualified} finished  ·  💀 {eliminated} out"
    else:
        stats_line = f"👥 {alive_count} alive  ·  💀 {eliminated} out  ·  {total} total"

    details_block = f"\n\n{details}" if details else ""
    event_block = (
        f"\n\n📣 <b>Update:</b> {html.escape(str(event)[:140])}"
        if event and not finished else ""
    )
    results_block = _round_result_block(match, stage) if finished else ""

    return (
        f"{heading}\n"
        f"<b>{html.escape(title)}</b>\n"
        f"<i>{html.escape(description)}</i>\n\n"
        f"{status_line}\n"
        f"{stats_line}"
        f"{details_block}"
        f"{event_block}"
        f"{results_block}"
    )



def next_round_keyboard(match_id):
    return kb([[btn("✅ ENTER NEXT ROUND", f"nr:enter:{match_id}")]])


def next_round_gate_text(match, seconds_left=None):
    gate = match.get("next_round") or {}
    eligible = gate.get("eligible_ids") or []
    joined = gate.get("joined_ids") or []
    if seconds_left is None:
        seconds_left = max(0, int(float(gate.get("deadline", time.time())) - time.time() + 0.999))

    source_title = html.escape(str(gate.get("completed_title") or "Previous round"))
    from_stage = gate.get("from_stage")
    next_copy = {
        "rlgl": ("🍪 <b>UP NEXT · DALGONA CANDY</b>", "Watch the shared group pattern, then repeat it on the private numbered grid. One wrong tap or timeout eliminates you."),
        "dalgona": ("🎱 <b>UP NEXT · MARBLES</b>", "Play your paired opponent privately. Hide marbles, bet carefully and win to survive."),
        "marbles": ("🌉 <b>UP NEXT · GLASS BRIDGE</b>", "Choose left or right privately. A wrong glass tile eliminates you."),
        "bridge": ("🗳 <b>UP NEXT · VOTE OR FINAL</b>", "The next round depends on how many qualified players enter."),
        "vote": ("⚔️ <b>UP NEXT · SQUID GAME FINAL</b>", "Choose tactical combat moves. One champion remains."),
        "final": ("⚔️ <b>UP NEXT · FINAL BRACKET</b>", "Enter to continue into the next set of tactical duels."),
    }
    next_heading, next_description = next_copy.get(
        from_stage, ("🎮 <b>UP NEXT</b>", "Enter to continue the competition.")
    )
    if gate.get("status") != "open":
        action_line = "⏳ <b>Entry closed</b>"
    else:
        action_line = f"⏳ <b>{seconds_left}s left to enter</b>"

    results = (match.get("round_results") or {}).get(from_stage) or {}
    player_by_id = {str(p.get("id", p.get("user_id"))): p for p in (match.get("players") or [])}
    qualified_ids = results.get("qualified_ids", eligible)
    eliminated_ids = results.get("eliminated_ids", [])
    qualified_names = [
        html.escape(str((player_by_id.get(str(uid)) or {}).get("name") or uid)[:32])
        for uid in qualified_ids
    ]
    eliminated_names = [
        html.escape(str((player_by_id.get(str(uid)) or {}).get("name") or uid)[:32])
        for uid in eliminated_ids
    ]
    qualified_line = ", ".join(qualified_names) if qualified_names else "None"
    eliminated_line = ", ".join(eliminated_names) if eliminated_names else "None"

    # --- EASY TO EDIT UI FORMAT ---
    return (
        f"🦑 <b>ROUND RESULTS</b>\n"
        f"🏁 <b>{source_title}</b> complete\n"
        f"✅ Qualified: <b>{len(qualified_ids)}</b>\n"
        f"<i>{qualified_line}</i>\n"
        f"💀 Eliminated: <b>{len(eliminated_ids)}</b>\n"
        f"<i>{eliminated_line}</i>\n"
        f"\n"
        f"{next_heading}\n"
        f"<i>{next_description}</i>\n"
        f"\n"
        f"🎟 <b>Entry:</b> {len(joined)}/{len(eligible)} qualified players\n"
        f"Tap <b>ENTER NEXT ROUND</b> below to continue.\n"
        f"{action_line} · Anyone who does not enter is eliminated from the match."
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


def _round_announcement_html(stage, text):
    """Render round artwork and text as separate Rich Message blocks."""
    image_url = ROUND_ANNOUNCEMENT_IMAGES.get(
        str(stage or "").lower()
    )

    if not image_url:
        return text

    image_url = _validate_rich_image_url(image_url)
    image_block = (
        f'<img src="{html.escape(image_url, quote=True)}"/>'
    )

    paragraphs = []

    for line in str(text or "").splitlines():
        line = line.strip()

        if not line:
            continue

        # Preserve inline HTML formatting and create a real block.
        paragraphs.append(f"<p>{line}</p>")

    return image_block + "".join(paragraphs)


def _has_round_announcement_image(stage):
    return str(stage or "").lower() in ROUND_ANNOUNCEMENT_IMAGES


async def _send_round_message(bot, chat_id, stage, text, reply_markup=None):
    """Send a text announcement or an image+text Rich Message for mapped stages."""
    if _has_round_announcement_image(stage):
        rich_html = _round_announcement_html(stage, text)
        options = {}
        if reply_markup is not None:
            options["reply_markup"] = reply_markup
        result = await send_rich_message(bot, chat_id, rich_html, **options)
        message_id = (
            result.get("message_id") if isinstance(result, dict)
            else getattr(result, "message_id", None)
        )
        if not message_id:
            raise RuntimeError("Rich round announcement returned no message_id")
        from types import SimpleNamespace
        return SimpleNamespace(message_id=message_id, chat_id=chat_id)

    return await bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode="HTML",
        reply_markup=reply_markup,
        disable_web_page_preview=True,
    )


async def _edit_round_message(bot, chat_id, message_id, stage, text, reply_markup=None):
    """Edit using the same transport the original round message was sent with."""
    if _has_round_announcement_image(stage):
        rich_html = _round_announcement_html(stage, text)
        options = {}
        if reply_markup is not None:
            options["reply_markup"] = reply_markup
        return await edit_rich_message(
            bot, chat_id, message_id, rich_html, **options
        )

    return await bot.edit_message_text(
        chat_id=chat_id,
        message_id=message_id,
        text=text,
        parse_mode="HTML",
        reply_markup=reply_markup,
        disable_web_page_preview=True,
    )


async def send_round_announcement(bot, match, stage, details=None, event=None, reply_markup=None):
    """Always post a fresh announcement; mapped stages include their image inline."""
    text = round_status_text(match, stage, details=details, event=event)
    message = None
    for attempt in range(2):
        try:
            message = await _send_round_message(
                bot, match["chat_id"], stage, text, reply_markup=reply_markup
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


async def refresh_round_announcement(bot, match, stage=None, details=None, event=None, finished=False, reply_markup=None):
    stage = stage or match.get("round_message_stage") or match.get("stage", "unknown")
    text = round_status_text(match, stage, details=details, event=event, finished=finished)
    message_id = match.get("round_message_id")

    if not message_id:
        # A missing active card should not block gameplay announcements.
        message = await _send_round_message(
            bot, match["chat_id"], stage, text, reply_markup=reply_markup
        )
        match["round_message_id"] = message.message_id
        match["round_message_stage"] = stage
        from .repository import save_match
        await save_match(match)
        return True

    # Same message ko edit karega chahe game chal raha ho ya khatam ho gaya ho
    for attempt in range(2):
        try:
            await _edit_round_message(
                bot,
                match["chat_id"],
                message_id,
                stage,
                text,
                reply_markup=reply_markup,
            )
            return True
        except BadRequest as exc:
            if "message is not modified" in str(exc).lower():
                return True
            log.warning("Round announcement edit failed for %s: %s", stage, exc)
            return False
        except RetryAfter as exc:
            if attempt == 0:
                delay = getattr(exc, "retry_after", 1)
                await asyncio.sleep(float(getattr(delay, "total_seconds", lambda: delay)()) + 0.2)
            else:
                return False
        except TelegramError:
            log.exception("Round announcement edit failed for %s", stage)
            return False
    return False


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
        '<tg-button-row align="center">'
        f'<tg-button type="callback_data" style="primary" '
        f'data="rlgl:{safe_match_id}:{safe_phase}:left">⬅️ LEFT</tg-button>'
        f'<tg-button type="callback_data" style="primary" '
        f'data="rlgl:{safe_match_id}:{safe_phase}:right">RIGHT ➡️</tg-button>'
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