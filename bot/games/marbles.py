"""Game 3 — Marbles (odd or even)."""
import asyncio
import random
import html
import logging
from .. import config, runtime, ui
from ..manager import safe_edit, safe_answer, game_finished
from ..repository import get_match, save_match, alive, get_player, pname

PREPARATION_SECONDS = 30


def _other(pair, uid):
    return pair["b"] if uid == pair["a"] else pair["a"]


def _find_pair(match, uid):
    for pair in match["game"]["pairs"]:
        if uid in (pair["a"], pair["b"]):
            return pair
    return None


def _group_details(match, seconds_left=None):
    game = match.get("game") or {}
    pairs = game.get("pairs") or []
    lines = []
    if seconds_left is not None and seconds_left > 0:
        lines.append(f"⏳ <b>Round starts in {seconds_left}s…</b>")
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
    from ..manager import refresh_lobby_scoreboard
    await refresh_lobby_scoreboard(context, mid)


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
        pairs.append({
            "a": a, "b": b, "hider": random.choice([a, b]),
            "marbles": {str(a): config.MARBLES_START, str(b): config.MARBLES_START},
            "phase": "hide", "hidden": None, "bet": None, "winner": None,
        })
    if ids:
        bye = ids.pop()
    match["game"] = {"pairs": pairs, "bye": bye, "last_event": None, "round_finished": False}
    await ui.send_round_announcement(
        context.bot, match, "marbles",
        details=_group_details(match, PREPARATION_SECONDS),
    )
    await save_match(match)

    runtime.add_task(mid, asyncio.create_task(_run_round(context, mid)))


async def _run_round(context, mid):
    try:
        for delay, shown in [(10, 20), (10, 10), (5, 5)]:
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
        from .. import logging as _lg
        _lg.getLogger(__name__).exception("Marbles run_round crashed")


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


async def on_guess(update, context):
    q = update.callback_query
    _, _, mid, choice = q.data.split(":")
    uid = q.from_user.id
    rt = runtime.get(mid)

    error_msg = None
    alert = False
    result_text = None
    guesser = uid
    hider = None
    guesser_dm = None
    hider_dm = None
    trigger_check_end = False
    trigger_prompt_hide = False
    next_pair_state = None

    async with rt["lock"]:
        match = await get_match(mid)
        if not match or match["stage"] != "marbles":
            error_msg = ""
        else:
            pair = _find_pair(match, uid)
            if not pair or pair["phase"] != "guess" or pair["hider"] == uid:
                error_msg, alert = "Not your move!", True
            else:
                hider = pair["hider"]
                hidden, bet = pair["hidden"], pair["bet"]
                parity = "odd" if hidden % 2 else "even"
                guesser_wins = choice == parity
                if guesser_wins:
                    pair["marbles"][str(guesser)] += bet
                    pair["marbles"][str(hider)] -= bet
                else:
                    pair["marbles"][str(guesser)] -= bet
                    pair["marbles"][str(hider)] += bet

                result_text = (
                    f"🎱 Hidden: <b>{hidden}</b> ({parity}) — guess: <b>{choice.upper()}</b>\n"
                    f"{'✅ Guesser wins' if guesser_wins else '❌ Guesser loses'} {bet} marbles!\n"
                    f"{html.escape(pname(match, guesser))}: {pair['marbles'][str(guesser)]} 🎱  |  "
                    f"{html.escape(pname(match, hider))}: {pair['marbles'][str(hider)]} 🎱"
                )
                guesser_dm = get_player(match, guesser).get("dm_mid")
                hider_dm = get_player(match, hider).get("dm_mid")

                if pair["marbles"][str(guesser)] == 0 or pair["marbles"][str(hider)] == 0:
                    pair["winner"] = guesser if pair["marbles"][str(guesser)] > 0 else hider
                    pair["phase"] = "done"
                    loser = hider if pair["winner"] == guesser else guesser
                    get_player(match, loser)["alive"] = False
                    match["game"]["last_event"] = f"{pname(match, pair['winner'])} won their marbles match and advanced."
                    await save_match(match)
                    trigger_check_end = True
                else:
                    pair["hider"] = _other(pair, hider)
                    pair["phase"] = "hide"
                    pair["hidden"] = None
                    pair["bet"] = None
                    match["game"]["last_event"] = "A marble exchange was resolved; active pairings remain in progress."
                    await save_match(match)
                    next_pair_state = dict(pair)
                    trigger_prompt_hide = True

    if error_msg is not None:
        await safe_answer(q, error_msg, alert)
        return

    await safe_answer(q)
    
    # If the pair is done, clear the keyboards. Otherwise, keep them clear until _prompt_hide
    is_done = False
    async with rt["lock"]:
        match_check = await get_match(mid)
        if match_check:
            pair_check = _find_pair(match_check, uid)
            if pair_check and pair_check.get("phase") == "done":
                is_done = True

    markup = ui.kb([]) if is_done else None

    if guesser_dm:
        await safe_edit(context.bot, guesser, guesser_dm, result_text, reply_markup=markup)
    if hider_dm:
        await safe_edit(context.bot, hider, hider_dm, result_text, reply_markup=markup)

    await _refresh_group(context, mid)

    if trigger_check_end:
        await _check_end(context, mid)
    elif trigger_prompt_hide:
        # Thoda pause taaki players result padh sakein naye prompt aane se pehle
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