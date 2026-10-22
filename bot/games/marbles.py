"""Game 3 — Marbles (odd or even)."""
import asyncio
import html
import logging
import random
from .. import config, runtime, ui
from ..manager import safe_edit, safe_answer, game_finished
from ..repository import get_match, save_match, alive, get_player, pname

PREPARATION_SECONDS = 15


def _other(pair, uid):
    return pair["b"] if uid == pair["a"] else pair["a"]


def _find_pair(match, uid):
    for pair in match["game"]["pairs"]:
        if uid in (pair["a"], pair["b"]):
            return pair
    return None


def _new_pair(player_a, player_b):
    """Create the complete initial state for one marbles pairing."""
    return {
        "a": player_a,
        "b": player_b,
        "hider": random.choice((player_a, player_b)),
        "marbles": {
            str(player_a): config.MARBLES_START,
            str(player_b): config.MARBLES_START,
        },
        "phase": "hide",
        "hidden": None,
        "bet": None,
        "winner": None,
    }


def _group_details(match, seconds_left=None):
    game = match.get("game") or {}
    pairs = game.get("pairs") or []
    lines = []
    if seconds_left is not None:
        lines.append(ui.round_waiting_panel(
            "marbles", seconds_left, total_seconds=PREPARATION_SECONDS,
        ))
        lines.append("")
    lines.append("🎱 <b>PAIR STATUS</b>")
    if not pairs:
        lines.append("No active pairings.")
    else:
        for pair in pairs[:12]:
            a_name = html.escape(pname(match, pair["a"]))
            b_name = html.escape(pname(match, pair["b"]))
            if pair.get("winner") is not None:
                label = f"🏅 {html.escape(pname(match, pair['winner']))} advanced"
            elif pair.get("phase") == "done":
                label = "✅ Complete"
            else:
                label = "🎲 In progress"
            lines.append(f"• {a_name} vs {b_name} — {label}")
        if len(pairs) > 12:
            lines.append(f"…and {len(pairs) - 12} more pairs")
    if game.get("bye"):
        lines.append(f"🎟 {html.escape(pname(match, game['bye']))} receives a bye.")
    lines.append(
        f"⏱ <b>Time limit:</b> {config.MARBLES_SECONDS}s · "
        f"Each player starts with {config.MARBLES_START} 🎱"
    )
    return "\n".join(lines)


async def _refresh_group(context, mid, event=None, finished=False, seconds_left=None):
    rt = runtime.get(mid)
    async with rt["lock"]:
        match = await get_match(mid)
        if not match or match.get("stage") != "marbles":
            return
        game = match.get("game") or {}
        if event:
            game["last_event"] = event
            await save_match(match)
    await ui.refresh_round_announcement(
        context.bot, match, "marbles", details=_group_details(match, seconds_left),
        event=game.get("last_event"), finished=finished,
    )
    if finished:
        from ..manager import refresh_lobby_scoreboard
        await refresh_lobby_scoreboard(context, mid, force=True)


async def begin(context, mid):
    match = await get_match(mid)
    if not match:
        return
    players = alive(match)
    random.shuffle(players)
    ids = [p["id"] for p in players]
    pairs, bye = [], None
    while len(ids) >= 2:
        a, b = ids.pop(), ids.pop()
        pairs.append(_new_pair(a, b))
    if ids:
        bye = ids.pop()
    match["game"] = {"pairs": pairs, "bye": bye, "last_event": None, "round_finished": False}

    async def _send_preparation_dm(player):
        text = "🎱 <b>Marbles is preparing!</b>\n\nWait for the group countdown to finish..."
        if bye and player["id"] == bye:
            text = "🎟 <b>You received a BYE!</b>\n\nYou automatically advance to the next round. Please wait for others to finish."
        try:
            message = await context.bot.send_message(
                chat_id=player["id"],
                text=text,
                parse_mode="HTML",
            )
            player["dm_mid"] = message.message_id
        except Exception:
            # Preserve the existing rule: players we cannot DM are removed.
            player["alive"] = False

    # Concurrent sends reduce the preparation delay from the sum of request times
    # to approximately the slowest individual request. Save after IDs are assigned.
    await asyncio.gather(*(_send_preparation_dm(player) for player in players))
    # --------------------------------

    await ui.send_round_announcement(
        context.bot, match, "marbles",
        details=_group_details(match, PREPARATION_SECONDS),
    )
    await save_match(match)

    runtime.add_task(mid, asyncio.create_task(_run_round(context, mid)))


async def _run_round(context, mid):
    try:
        for delay, shown in [(5, 10), (5, 5)]:
            await asyncio.sleep(delay)
            latest = await get_match(mid)
            if not latest or latest.get("stage") != "marbles":
                return
            await ui.refresh_round_announcement(
                context.bot, latest, "marbles",
                details=_group_details(latest, shown),
            )
        await asyncio.sleep(5)

        latest = await get_match(mid)
        if not latest or latest.get("stage") != "marbles":
            return
        await _refresh_group(context, mid)  # clear prep line
        for pair in latest["game"]["pairs"]:
            await _prompt_hide(context, mid, pair)
        runtime.add_task(mid, asyncio.create_task(_timeout(context, mid)))
    except asyncio.CancelledError:
        raise
    except Exception:
        logging.getLogger(__name__).exception("Marbles run_round crashed")


async def _prompt_hide(context, mid, pair):
    rt = runtime.get(mid)
    async with rt["lock"]:
        match = await get_match(mid)
        if not match or match.get("stage") != "marbles":
            return
        hider, guesser = pair["hider"], _other(pair, pair["hider"])
        hider_marbles = pair["marbles"][str(hider)]
        guesser_marbles = pair["marbles"][str(guesser)]
        hider_name = html.escape(pname(match, hider))
        
        hider_player = get_player(match, hider)
        guesser_player = get_player(match, guesser)
        hider_dm = hider_player.get("dm_mid") if hider_player else None
        guesser_dm = guesser_player.get("dm_mid") if guesser_player else None

    # HIDER DM
    hider_text = (
        f"🎱 You are the <b>HIDER</b> (you: {hider_marbles} 🎱 | "
        f"foe: {guesser_marbles} 🎱)\nHow many marbles do you hide in your fist?"
    )
    hider_markup = ui.marbles_count_keyboard(mid, "hide", hider_marbles)
    
    if hider_dm:
        await safe_edit(context.bot, hider, hider_dm, hider_text, reply_markup=hider_markup)
    else:
        try:
            message = await context.bot.send_message(
                hider, hider_text, reply_markup=hider_markup, parse_mode="HTML"
            )
            async with rt["lock"]:
                match = await get_match(mid)
                get_player(match, hider)["dm_mid"] = message.message_id
                await save_match(match)
        except Exception:
            pass

    # GUESSER DM
    guesser_text = f"🎱 {hider_name} is hiding marbles… get ready to bet!"
    if guesser_dm:
        await safe_edit(context.bot, guesser, guesser_dm, guesser_text, reply_markup=ui.kb([]))
    else:
        try:
            message = await context.bot.send_message(
                guesser, guesser_text, parse_mode="HTML"
            )
            async with rt["lock"]:
                match = await get_match(mid)
                get_player(match, guesser)["dm_mid"] = message.message_id
                await save_match(match)
        except Exception:
            pass


async def on_hide(update, context):
    q = update.callback_query
    _, _, mid, n = q.data.split(":")
    n = int(n)
    uid = q.from_user.id
    rt = runtime.get(mid)

    error_msg = None
    alert = False
    guesser = None
    max_bet = 0
    hider_dm = None
    guesser_dm = None

    async with rt["lock"]:
        match = await get_match(mid)
        if not match or match["stage"] != "marbles":
            error_msg = ""
        else:
            pair = _find_pair(match, uid)
            if not pair or pair["phase"] != "hide" or pair["hider"] != uid:
                error_msg, alert = "Not your move!", True
            else:
                pair["hidden"] = n
                pair["phase"] = "bet"
                await save_match(match)
                guesser = _other(pair, uid)
                max_bet = min(pair["marbles"][str(guesser)], pair["marbles"][str(pair["hider"])])
                hider_dm = get_player(match, pair["hider"]).get("dm_mid")
                guesser_dm = get_player(match, guesser).get("dm_mid")

    if error_msg is not None:
        await safe_answer(q, error_msg, alert)
        return

    await safe_answer(q, f"You hid {n} 🎱")
    if hider_dm:
        await safe_edit(context.bot, uid, hider_dm, f"🎱 You hid <b>{n}</b>. Waiting for the guesser…")
    if guesser_dm:
        await safe_edit(
            context.bot, guesser, guesser_dm,
            f"🎲 Partner hid their marbles! Place your bet (1–{max_bet}):",
            reply_markup=ui.marbles_count_keyboard(mid, "bet", max_bet),
        )
    await _refresh_group(context, mid)


async def on_bet(update, context):
    q = update.callback_query
    _, _, mid, n = q.data.split(":")
    n = int(n)
    uid = q.from_user.id
    rt = runtime.get(mid)

    error_msg = None
    alert = False
    guesser_dm = None

    async with rt["lock"]:
        match = await get_match(mid)
        if not match or match["stage"] != "marbles":
            error_msg = ""
        else:
            pair = _find_pair(match, uid)
            if not pair or pair["phase"] != "bet" or pair["hider"] == uid:
                error_msg, alert = "Not your move!", True
            else:
                pair["bet"] = n
                pair["phase"] = "guess"
                await save_match(match)
                guesser_dm = get_player(match, uid).get("dm_mid")

    if error_msg is not None:
        await safe_answer(q, error_msg, alert)
        return

    await safe_answer(q, f"Bet: {n} 🎱")
    if guesser_dm:
        await safe_edit(
            context.bot, uid, guesser_dm,
            f"🎲 Bet: <b>{n} 🎱</b> — is the hidden amount ODD or EVEN?",
            reply_markup=ui.oddeven_keyboard(mid),
        )
    await _refresh_group(context, mid)


def _guess_result_text(match, pair, guesser, hider, choice, hidden, bet, guesser_wins):
    parity = "odd" if hidden % 2 else "even"
    result_label = "✅ Guesser wins" if guesser_wins else "❌ Guesser loses"
    return (
        f"🎱 Hidden: <b>{hidden}</b> ({parity}) — guess: <b>{choice.upper()}</b>\n"
        f"{result_label} {bet} marbles!\n"
        f"{html.escape(pname(match, guesser))}: {pair['marbles'][str(guesser)]} 🎱  |  "
        f"{html.escape(pname(match, hider))}: {pair['marbles'][str(hider)]} 🎱"
    )


def _apply_guess(match, pair, guesser, choice):
    """Apply a parity guess and advance the pair's state machine."""
    hider = pair["hider"]
    hidden, bet = pair["hidden"], pair["bet"]
    guesser_key, hider_key = str(guesser), str(hider)
    parity = "odd" if hidden % 2 else "even"
    guesser_wins = choice == parity

    if guesser_wins:
        pair["marbles"][guesser_key] += bet
        pair["marbles"][hider_key] -= bet
    else:
        pair["marbles"][guesser_key] -= bet
        pair["marbles"][hider_key] += bet

    result_text = _guess_result_text(
        match, pair, guesser, hider, choice, hidden, bet, guesser_wins
    )
    if pair["marbles"][guesser_key] == 0 or pair["marbles"][hider_key] == 0:
        pair["winner"] = guesser if pair["marbles"][guesser_key] > 0 else hider
        pair["phase"] = "done"
        loser = hider if pair["winner"] == guesser else guesser
        loser_player = get_player(match, loser)
        if loser_player:
            loser_player["alive"] = False
        match["game"]["last_event"] = (
            f"{pname(match, pair['winner'])} won their marbles match and advanced."
        )
        return result_text, hider, True, None

    pair["hider"] = _other(pair, hider)
    pair["phase"] = "hide"
    pair["hidden"] = None
    pair["bet"] = None
    match["game"]["last_event"] = (
        "A marble exchange was resolved; active pairings remain in progress."
    )
    return result_text, hider, False, dict(pair)


async def on_guess(update, context):
    query = update.callback_query
    _, _, mid, choice = query.data.split(":")
    guesser = query.from_user.id
    rt = runtime.get(mid)

    error_msg = None
    alert = False
    result_text = None
    hider = None
    guesser_dm = None
    hider_dm = None
    pair_finished = False
    next_pair_state = None

    async with rt["lock"]:
        match = await get_match(mid)
        if not match or match.get("stage") != "marbles":
            error_msg = ""
        else:
            pair = _find_pair(match, guesser)
            if not pair or pair["phase"] != "guess" or pair["hider"] == guesser:
                error_msg, alert = "Not your move!", True
            else:
                result_text, hider, pair_finished, next_pair_state = _apply_guess(
                    match, pair, guesser, choice
                )
                guesser_player = get_player(match, guesser)
                hider_player = get_player(match, hider)
                guesser_dm = guesser_player.get("dm_mid") if guesser_player else None
                hider_dm = hider_player.get("dm_mid") if hider_player else None
                await save_match(match)

    if error_msg is not None:
        await safe_answer(query, error_msg, alert)
        return

    await safe_answer(query)
    markup = ui.kb([]) if pair_finished else None
    if guesser_dm:
        await safe_edit(
            context.bot, guesser, guesser_dm, result_text, reply_markup=markup
        )
    if hider_dm:
        await safe_edit(
            context.bot, hider, hider_dm, result_text, reply_markup=markup
        )

    await _refresh_group(context, mid)
    if pair_finished:
        await _check_end(context, mid)
    elif next_pair_state:
        # Give both players a moment to read the result before the next prompt.
        await asyncio.sleep(2.0)
        await _prompt_hide(context, mid, next_pair_state)


async def _check_end(context, mid):
    rt = runtime.get(mid)
    is_done = False
    async with rt["lock"]:
        match = await get_match(mid)
        if match and match.get("stage") == "marbles" and all(pair["phase"] == "done" for pair in match["game"]["pairs"]):
            is_done = True
    if is_done:
        await _finish(context, mid)


async def _timeout(context, mid):
    try:
        await asyncio.sleep(config.MARBLES_SECONDS)
    except asyncio.CancelledError:
        return
    match = await get_match(mid)
    if match and match.get("stage") == "marbles":
        await _finish(context, mid, timed_out=True)


async def _finish(context, mid, timed_out=False):
    rt = runtime.get(mid)
    survivors = []

    async with rt["lock"]:
        match = await get_match(mid)
        if not match or match.get("stage") != "marbles" or match.get("game", {}).get("round_finished"):
            return
        game = match["game"]
        if timed_out:
            for pair in game["pairs"]:
                if pair.get("phase") != "done":
                    pair["phase"] = "done"
                    pair["winner"] = None
                    for uid in (pair["a"], pair["b"]):
                        player = get_player(match, uid)
                        if player:
                            player["alive"] = False
            game["last_event"] = "Time expired. Players in unfinished pairs were eliminated."
        game["round_finished"] = True
        survivors = [pair["winner"] for pair in game["pairs"] if pair.get("winner") is not None]
        if game.get("bye"):
            survivors.append(game["bye"])
        await save_match(match)

    await ui.refresh_round_announcement(
        context.bot, match, "marbles", details=_group_details(match),
        event=match["game"].get("last_event"), finished=True,
    )
    runtime.cancel_tasks(mid)
    await game_finished(context, mid, survivors, "Marbles")