"""Dalgona Candy: Sequential group shape rounds, private numbered-grid input."""
from __future__ import annotations

import asyncio
import logging
import random
import time

from telegram.error import BadRequest, RetryAfter, TelegramError

from .. import runtime, ui, config
from ..manager import safe_answer, safe_edit, game_finished, refresh_lobby_scoreboard
from ..repository import get_match, save_match, alive, get_player, update_dalgona_tap

log = logging.getLogger(__name__)

# Sequential Group Shapes (Shape Name, Sequence Length, Emoji)
GROUP_SHAPES = [
    ("Circle", 4, "⭕"),
    ("Triangle", 6, "🔺"),
    ("Star", 8, "⭐"),
    ("Umbrella", 10, "☂️"),
]

# Keep group-grid edits below roughly 20 group-visible mutations/minute. Telegram may
# still return RetryAfter; in that case the exact same edit is retried before advancing.
FLASH_SECONDS = 1
DM_READY_SECONDS = 5
NEXT_PATTERN_PAUSE_SECONDS = 5
PREPARATION_SECONDS = 30


def _state_lock(mid: str) -> asyncio.Lock:
    """Return the per-match Dalgona mutation lock."""
    state = runtime.get(str(mid))
    lock = state.get("dalgona_state_lock")
    if lock is None:
        lock = state["dalgona_state_lock"] = asyncio.Lock()
    return lock


def _input_seconds(sequence_length: int) -> int:
    """Dynamically calculate time based on sequence length."""
    return max(8, min(25, 5 + int(sequence_length * 1.5)))


def _player_list_html(players: list[dict]) -> str:
    """Generate hyperlinked player list for Msg-1."""
    links = []
    for p in players:
        if p.get("alive"):
            uid = p.get("id")
            name = ui.html.escape(p.get("name") or str(uid))
            links.append(f'<a href="tg://user?id={uid}">{name}</a>')
    return ", ".join(links) if links else "None"


def _group_details(match: dict, seconds_left: int = None) -> str:
    """Render the dynamic content for the persistent group card (Msg-1)."""
    game = match.get("game") or {}
    players = match.get("players", [])
    alive_players = [p for p in players if p.get("alive")]
    
    phase = game.get("sequence_phase")
    shape_idx = game.get("current_shape_index", 0)
    
    text = []
    timer_text = None
    
    if phase == "preparation":
        text.append(f"👥 <b>Players ({len(alive_players)}):</b>\n{_player_list_html(alive_players)}")
        timer_text = f"⏳ <b>Round starts in {seconds_left or 0}s</b>"
    elif phase == "showing":
        shape_name, _, emoji = GROUP_SHAPES[shape_idx]
        text.append(f"🧠 <b>Watch the flashing grid closely!</b>")
        text.append(f"Current Shape: <b>{emoji} {shape_name}</b>")
    elif phase == "transition":
        text.append("🚶 <b>Switch to your DM!</b> The grid will appear shortly.")
    elif phase in ("ready", "input"):
        text.append("🎯 <b>Perform the sequence in your DM now!</b>")
    elif phase == "resolved":
        text.append("📊 <b>Results Updated!</b> Next round starting soon...")
        
    # Ye timer ko last me 2 line ke gap ke sath add karega
    if timer_text:
        text.extend(["", "", timer_text])
        
    return "\n".join(text)


async def _refresh_group(
    context,
    mid: str,
    event: str | None = None,
    finished: bool = False,
    seconds_left: int = None,
) -> None:
    """Refresh the round card at meaningful checkpoints, not on every flash.

    The flashing board is already a separate group message. Editing both the
    round card and the match scoreboard for every phase needlessly increases
    group API traffic, so the scoreboard is refreshed only for results/final.
    """
    match = await get_match(mid)
    if not match or match.get("stage") != "dalgona":
        return

    game = match.setdefault("game", {})
    if event:
        game["last_event"] = event
        await save_match(match)

    phase = game.get("sequence_phase")
    update_round_card = bool(event or finished or phase in {"preparation", "resolved"})
    if update_round_card:
        await ui.refresh_round_announcement(
            context.bot,
            match,
            "dalgona",
            details=_group_details(match, seconds_left),
            event=game.get("last_event") if event else None,
            finished=finished,
        )

    # Do not edit the scoreboard at every preparation tick or between the
    # individual tile flashes. Results are the useful point to refresh it.
    if event or finished:
        await refresh_lobby_scoreboard(context, mid, force=finished)


async def _set_board(
    context, match: dict, shape_name: str, emoji: str,
    sequence_length: int, first_cell: int,
) -> int:
    """Send the temporary flashing grid, showing the first tile immediately."""
    markup = ui.dalgona_display_keyboard(
        str(match["_id"]), sequence_length, active_cell=first_cell
    )
    text = (
        f"🍪 <b>DALGONA · {emoji} {shape_name.upper()}</b>\n\n"
        f"Watch all {sequence_length} highlighted tiles in order.\n"
        "After the grid disappears, use the next message's DM button to repeat the pattern."
    )
    message = await context.bot.send_message(
        chat_id=match["chat_id"], text=text, parse_mode="HTML",
        reply_markup=markup, disable_web_page_preview=True,
    )
    return message.message_id


async def _edit_board_markup(
    context,
    chat_id: int,
    board_id: int,
    mid: str,
    seq_len: int,
    active_cell: int | None,
) -> bool:
    """Edit one flash; never advance past a tile that Telegram has not accepted.

    RetryAfter includes the server-requested wait. Retrying the same markup
    after that wait preserves the sequence instead of silently skipping a tile.
    False means a non-rate-limit failure occurred and the caller must restart
    the full pattern rather than ask players to reproduce an unseen sequence.
    """
    markup = ui.dalgona_display_keyboard(mid, seq_len, active_cell=active_cell)

    while True:
        try:
            await context.bot.edit_message_reply_markup(
                chat_id=chat_id,
                message_id=board_id,
                reply_markup=markup,
            )
            return True
        except RetryAfter as exc:
            delay = getattr(exc, "retry_after", 1)
            if hasattr(delay, "total_seconds"):
                delay = delay.total_seconds()
            delay = max(0.0, float(delay))
            log.warning(
                "Dalgona flash rate-limited for cell %s; retrying the same tile in %.2fs",
                active_cell,
                delay,
            )
            await asyncio.sleep(delay + 0.1)
            # Intentionally retry the SAME tile before the sequence advances.
        except BadRequest as exc:
            if "message is not modified" in str(exc).lower():
                return True
            log.warning(
                "Dalgona flash update failed for cell %s: %s",
                active_cell,
                exc,
            )
            return False
        except TelegramError as exc:
            log.warning(
                "Dalgona flash update failed for cell %s: %s",
                active_cell,
                exc,
            )
            return False


def _new_sequence(length: int) -> list[int]:
    """Generate a sequence without adjacent repeats that would look like a stuck flash."""
    sequence: list[int] = []
    for _ in range(length):
        choices = [cell for cell in range(1, 10) if not sequence or cell != sequence[-1]]
        sequence.append(random.choice(choices))
    return sequence


async def _delete_previous_dm_prompt(context, mid: str) -> None:
    """Keep only the current pattern's group-to-DM instruction below the game card."""
    message_id = None
    async with _state_lock(mid):
        match = await get_match(mid)
        if not match:
            return
        game = match.get("game") or {}
        message_id = game.get("dm_prompt_message_id")
        if not message_id:
            return
        game["dm_prompt_message_id"] = None
        await save_match(match)

    try:
        await context.bot.delete_message(chat_id=match["chat_id"], message_id=message_id)
    except Exception:
        log.debug("Could not remove previous Dalgona DM prompt %s", message_id, exc_info=True)


async def _send_dm_prompt(context, mid: str, shape_name: str, emoji: str, sequence_length: int) -> None:
    """Post a clear group CTA with a URL button that opens the bot's private chat."""
    bot_username = getattr(context.bot, "username", None)
    if not bot_username:
        try:
            bot_username = (await context.bot.get_me()).username
        except Exception:
            log.exception("Could not resolve bot username for Dalgona DM button")

    current = await get_match(mid)
    if not current or current.get("stage") != "dalgona":
        return

    markup = ui.open_dm_keyboard(bot_username, label="📩 OPEN BOT DM & PLAY")
    text = (
        f"✅ <b>{emoji} {shape_name.upper()} PATTERN SHOWN</b>\n\n"
        "<b>Now open the bot's private chat.</b>\n"
        f"Tap the numbered tiles <b>1–9</b> in the same order ({sequence_length} taps).\n"
        "❌ One wrong tile breaks your candy. Your controls activate shortly."
    )
    message = await context.bot.send_message(
        chat_id=current["chat_id"],
        text=text,
        parse_mode="HTML",
        reply_markup=markup,
        disable_web_page_preview=True,
    )

    async with _state_lock(mid):
        latest = await get_match(mid)
        if latest and latest.get("stage") == "dalgona":
            latest.setdefault("game", {})["dm_prompt_message_id"] = message.message_id
            await save_match(latest)
        else:
            try:
                await context.bot.delete_message(chat_id=message.chat_id, message_id=message.message_id)
            except Exception:
                pass


async def begin(context, mid: str) -> None:
    """Initialize the Dalgona round."""
    match = await get_match(mid)
    if not match or match.get("stage") != "dalgona": return

    players = alive(match)
    player_states = {
        str(p["id"]): {"progress": 0, "phase": "preparation", "dm_mid": None, "done": False}
        for p in players
    }

    match["game"] = {
        "players": player_states,
        "last_event": None,
        "round_finished": False,
        "current_shape_index": random.randint(0, len(GROUP_SHAPES) - 1),
        "current_sequence": [],
        "sequence_phase": "preparation",
        "input_seconds": None,
        "dm_prompt_message_id": None,
    }
    await save_match(match)
    
    # Establish persistent Msg-1
    await ui.send_round_announcement(context.bot, match, "dalgona", details=_group_details(match, PREPARATION_SECONDS))
    
    # Pre-send basic DM to store dm_mid
    for p in players:
        try:
            msg = await context.bot.send_message(
                chat_id=p["id"],
                text="🍪 <b>Dalgona has started!</b>\n\nWatch the group chat for the pattern.",
                parse_mode="HTML"
            )
            match["game"]["players"][str(p["id"])]["dm_mid"] = msg.message_id
        except Exception:
            p["alive"] = False
            match["game"]["players"][str(p["id"])]["phase"] = "dead"
    await save_match(match)

    runtime.add_task(mid, asyncio.create_task(_run_game(context, mid)))


async def _run_game(context, mid: str) -> None:
    """Coordinate each Dalgona pattern without skipping failed flash edits."""
    pending_pattern: tuple[int, list[int]] | None = None
    flash_restarts: dict[int, int] = {}

    try:
        checkpoints = [30, 20, 10, 5]
        for index, remaining in enumerate(checkpoints):
            await _refresh_group(context, mid, seconds_left=remaining)
            next_remaining = checkpoints[index + 1] if index + 1 < len(checkpoints) else 0
            await asyncio.sleep(remaining - next_remaining)

        while True:
            await _delete_previous_dm_prompt(context, mid)
            async with _state_lock(mid):
                match = await get_match(mid)
                if not match or match.get("stage") != "dalgona":
                    return
                game = match.get("game") or {}
                if game.get("round_finished"):
                    return

                shape_idx = int(game.get("current_shape_index", 0))
                alive_players = [
                    player for player in match.get("players", [])
                    if player.get("alive", True)
                ]

                if not alive_players:
                    await _finish(context, mid, [])
                    return

                shape_name, seq_len, emoji = GROUP_SHAPES[shape_idx]
                if pending_pattern and pending_pattern[0] == shape_idx:
                    sequence = list(pending_pattern[1])
                else:
                    sequence = _new_sequence(seq_len)

                game.update({
                    "current_sequence": sequence,
                    "sequence_phase": "showing",
                    "input_seconds": None,
                })

                player_states = game.setdefault("players", {})
                for player in alive_players:
                    state = player_states.get(str(player["id"]))
                    if not isinstance(state, dict):
                        player["alive"] = False
                        continue
                    state.update({"progress": 0, "phase": "showing", "done": False})
                await save_match(match)

            alive_players = [
                player for player in match.get("players", [])
                if player.get("alive", True)
            ]
            if not alive_players:
                await _finish(context, mid, [])
                return

            await _refresh_group(context, mid)
            try:
                board_id = await _set_board(
                    context, match, shape_name, emoji, seq_len, first_cell=sequence[0]
                )
            except RetryAfter as exc:
                delay = getattr(exc, "retry_after", 1)
                await asyncio.sleep(max(0.0, float(getattr(delay, "total_seconds", lambda: delay)())) + 0.1)
                pending_pattern = (shape_idx, sequence)
                continue

            flash_ok = True
            next_flash_at = time.monotonic() + FLASH_SECONDS
            for cell in sequence[1:]:
                delay = next_flash_at - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)

                flash_started = time.monotonic()
                if not await _edit_board_markup(
                    context, match["chat_id"], board_id, mid, seq_len, cell
                ):
                    flash_ok = False
                    break
                next_flash_at = flash_started + FLASH_SECONDS

            if not flash_ok:
                try:
                    await context.bot.delete_message(
                        chat_id=match["chat_id"], message_id=board_id
                    )
                except TelegramError:
                    pass

                attempts = flash_restarts.get(shape_idx, 0) + 1
                flash_restarts[shape_idx] = attempts
                pending_pattern = (shape_idx, sequence)
                if attempts <= 2:
                    await _refresh_group(
                        context,
                        mid,
                        event=f"⚠️ Telegram could not update the {shape_name} grid. The same pattern will restart.",
                    )
                    await asyncio.sleep(2.0)
                    continue

                latest = await get_match(mid)
                survivors = [
                    player["id"] for player in (latest or {}).get("players", [])
                    if player.get("alive", True)
                ]
                await _finish(context, mid, survivors)
                return

            pending_pattern = None
            flash_restarts.pop(shape_idx, None)

            try:
                await context.bot.delete_message(
                    chat_id=match["chat_id"], message_id=board_id
                )
            except TelegramError:
                pass

            input_seconds = _input_seconds(seq_len)

            async with _state_lock(mid):
                match = await get_match(mid)
                if not match or match.get("stage") != "dalgona":
                    return
                game = match.setdefault("game", {})
                game["sequence_phase"] = "ready"
                await save_match(match)

            await _refresh_group(context, mid)
            await _send_dm_prompt(context, mid, shape_name, emoji, seq_len)

            alive_players = [
                player for player in match.get("players", [])
                if player.get("alive", True)
                and isinstance((match.get("game", {}).get("players", {}) or {}).get(str(player.get("id"))), dict)
            ]
            dm_actions = [
                safe_edit(
                    context.bot,
                    player["id"],
                    match["game"]["players"][str(player["id"])].get("dm_mid"),
                    f"🍪 <b>{emoji} {shape_name.upper()}</b>\n\nGet ready... controls open in {DM_READY_SECONDS}s!",
                    reply_markup=ui.dalgona_keyboard(mid, shape_idx),
                )
                for player in alive_players
            ]
            await asyncio.gather(*dm_actions, return_exceptions=True)
            await asyncio.sleep(DM_READY_SECONDS)

            async with _state_lock(mid):
                match = await get_match(mid)
                if not match or match.get("stage") != "dalgona":
                    return
                game = match.setdefault("game", {})
                game["sequence_phase"] = "input"
                game["input_seconds"] = input_seconds
                player_states = game.get("players", {}) or {}
                for player in alive_players:
                    state = player_states.get(str(player["id"]))
                    if isinstance(state, dict):
                        state["phase"] = "input"
                await save_match(match)

                event = asyncio.Event()
                runtime.get(mid)["dalgona_wave_event"] = event

            input_actions = [
                safe_edit(
                    context.bot,
                    player["id"],
                    match["game"]["players"][str(player["id"])].get("dm_mid"),
                    f"🚦 <b>GO!</b> You have {input_seconds}s to repeat the {seq_len}-tap pattern.",
                    reply_markup=ui.dalgona_keyboard(mid, shape_idx),
                )
                for player in alive_players
                if isinstance(match["game"].get("players", {}).get(str(player["id"])), dict)
            ]
            await asyncio.gather(*input_actions, return_exceptions=True)

            try:
                # Wait for either the timeout or all players to finish via the event
                await asyncio.wait_for(event.wait(), timeout=input_seconds)
            except asyncio.TimeoutError:
                # Expected when time runs out. The logic below handles it safely.
                pass
            except Exception as e:
                log.exception(f"Unexpected error during Dalgona timer: {e}")

            timed_out_count = 0
            async with _state_lock(mid):
                match = await get_match(mid)
                if not match or match.get("stage") != "dalgona":
                    return
                game = match.setdefault("game", {})
                player_states = game.get("players", {}) or {}

                # Eliminate anyone who is alive but did not finish
                for player in match.get("players", []):
                    if not player.get("alive", True):
                        continue
                    state = player_states.get(str(player.get("id")))
                    if not isinstance(state, dict) or not state.get("done"):
                        player["alive"] = False
                        if isinstance(state, dict):
                            state["phase"] = "dead"
                            dm_mid = state.get("dm_mid")
                        else:
                            dm_mid = None
                        timed_out_count += 1
                        if dm_mid:
                            asyncio.create_task(
                                safe_edit(
                                    context.bot,
                                    player["id"],
                                    dm_mid,
                                    "💀 <b>TIME'S UP!</b> Candy broken.",
                                )
                            )

                game["sequence_phase"] = "resolved"
                runtime.get(mid)["dalgona_wave_event"] = None
                await save_match(match)

            # Get final survivors
            survivors = [
                player for player in match.get("players", [])
                if player.get("alive", True)
            ]
            
            result_str = (
                f"<b>{emoji} {shape_name} Results:</b>\n"
                f"✅ {len(survivors)} Survived\n"
                f"💀 {timed_out_count} Eliminated"
            )
            # Event ko update karein taaki msg-1 me dikhe
            await _refresh_group(context, mid, event=result_str, finished=True)
            await asyncio.sleep(2.0)  # Thoda aur lamba pause dein

            # Single shape mode: Finish immediately
            survivor_ids = [p["id"] for p in survivors]
            await _finish(context, mid, survivor_ids)
            return

    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("Dalgona coordinator failed")

async def on_press(update, context) -> None:
    """Validate callbacks and safely process private-grid taps."""
    query = getattr(update, "callback_query", None)
    if not query:
        return

    parts = (query.data or "").split(":")
    if len(parts) != 4 or parts[0] != "dal":
        await safe_answer(query, "Invalid Dalgona button.", True)
        return

    _, mid, round_text, cell_text = parts
    try:
        round_index = int(round_text)
        cell = int(cell_text)
    except (TypeError, ValueError):
        await safe_answer(query, "Invalid tile. Please use the numbered grid.", True)
        return

    if not 1 <= cell <= 9:
        await safe_answer(query, "Invalid tile. Choose a number from 1 to 9.", True)
        return

    uid = query.from_user.id

    async with _state_lock(mid):
        match = await get_match(mid)
        if not match or match.get("stage") != "dalgona":
            await safe_answer(query, "This Dalgona round is no longer active.", True)
            return

        game = match.get("game") or {}
        if game.get("sequence_phase") != "input":
            await safe_answer(query, "Controls are not active yet!", True)
            return

        try:
            current_round = int(game.get("current_shape_index", -1))
        except (TypeError, ValueError):
            current_round = -1
        if round_index != current_round:
            await safe_answer(query, "These buttons are from an earlier pattern.", True)
            return

        player = get_player(match, uid)
        game_players = game.get("players") or {}
        state = game_players.get(str(uid)) if isinstance(game_players, dict) else None

        if not player or not player.get("alive", True):
            await safe_answer(query, "You are eliminated or not in this match.", True)
            return
        if not isinstance(state, dict):
            log.warning(
                "Dalgona player state missing during tap: match=%s user=%s",
                mid,
                uid,
            )
            await safe_answer(
                query,
                "Your game state is unavailable. Please wait for the next update.",
                True,
            )
            return
        if state.get("done"):
            await safe_answer(query, "You have already cleared this pattern. ✅", True)
            return

        sequence = game.get("current_sequence")
        if not isinstance(sequence, list) or not sequence:
            await safe_answer(query, "The pattern is unavailable. Please wait.", True)
            log.error("Dalgona input phase has no sequence (match=%s)", mid)
            return

        try:
            progress = int(state.get("progress", 0))
        except (TypeError, ValueError):
            await safe_answer(query, "Your progress state is invalid. Please wait.", True)
            log.error("Invalid Dalgona progress (match=%s user=%s)", mid, uid)
            return

        if progress < 0 or progress >= len(sequence):
            await safe_answer(query, "Your pattern state has expired. Please wait.", True)
            log.warning(
                "Dalgona progress out of bounds (match=%s user=%s progress=%s length=%s)",
                mid,
                uid,
                progress,
                len(sequence),
            )
            return

        if cell != sequence[progress]:
            player["alive"] = False
            state["phase"] = "dead"
            await safe_answer(query, "❌ Wrong tile! Your candy broke.", True)
            await safe_edit(
                context.bot,
                uid,
                state.get("dm_mid"),
                "💥💀 <b>ELIMINATED!</b> You tapped the wrong tile.",
            )
        else:
            state["progress"] = progress + 1
            if state["progress"] >= len(sequence):
                state["done"] = True
                await safe_answer(query, "✅ Pattern cleared!", True)
                await safe_edit(
                    context.bot,
                    uid,
                    state.get("dm_mid"),
                    "✅ <b>Pattern cleared!</b> Wait in the group for the next round.",
                )
            else:
                await safe_answer(query, f"✅ {state['progress']}/{len(sequence)}")

        # Persist only this player's relevant fields.
        await update_dalgona_tap(
            mid,
            uid,
            {"alive": player.get("alive", True)},
            {
                "progress": state["progress"],
                "done": state.get("done", False),
                "phase": state.get("phase", "input"),
            },
        )

        # RE-FETCH the match to get the live updated state of ALL players
        live_match = await get_match(mid)
        live_game_players = (live_match.get("game") or {}).get("players") or {}
        
        alive_players = [
            candidate for candidate in live_match.get("players", [])
            if candidate.get("alive", True)
        ]
        
        # Check if everyone who is still alive has completed the pattern
        all_alive_done = all(
            isinstance(live_game_players.get(str(candidate.get("id"))), dict)
            and live_game_players[str(candidate["id"])].get("done", False)
            for candidate in alive_players
        )
        
        if not alive_players or all_alive_done:
            event = runtime.get(mid).get("dalgona_wave_event")
            if event:
                event.set()


async def _finish(context, mid: str, survivors: list[int]) -> None:
    """Transition to next mini-game and ensure group receives the final results."""
    match = await get_match(mid)
    if not match or match.get("stage") != "dalgona":
        return
        
    # Mark the game as finished internally
    match.setdefault("game", {})["round_finished"] = True
    await save_match(match)
    
    # UI refresh ko time do taaki Telegram limit hit na kare aur result dikh sake
    await ui.refresh_round_announcement(context.bot, match, "dalgona", finished=True)
    await asyncio.sleep(1.5)  # Telegram API ko result show karne ka thoda time de
    
    # Manager ke game_finished ko call karein jo next round ka button banayega
    await game_finished(context, mid, survivors, "Dalgona Candy")