"""Game 6 — Squid Game final."""
import asyncio
import html
import logging
import random

from .. import config, runtime, ui
from ..manager import safe_edit, safe_answer, crown, open_final_bracket_gate, refresh_lobby_scoreboard
from ..repository import get_match, save_match, alive, get_player, pname

log = logging.getLogger(__name__)

PREPARATION_SECONDS = 15

BEATS = {"offense": "defense", "defense": "trickster", "trickster": "offense"}
LABEL = {"offense": "⚔️ Offensive", "defense": "🛡️ Defensive", "trickster": "🃏 Trickster"}


def _timeout_winner(duel):
    """Choose the advancing player when a duel misses its deadline."""
    player_a, player_b = str(duel["a"]), str(duel["b"])
    picks = duel.get("picks", {})
    if player_a in picks and player_b not in picks:
        return player_a, player_b
    if player_b in picks and player_a not in picks:
        return player_b, player_a
    return random.choice([(player_a, player_b), (player_b, player_a)])


def _duel_status_line(match, duel):
    """Render one duel's current state for the public bracket card."""
    player_a = html.escape(pname(match, duel["a"]))
    player_b = html.escape(pname(match, duel["b"]))
    if duel.get("winner") is not None:
        status = f"🏅 {html.escape(pname(match, duel['winner']))} advances"
    else:
        locked = len(duel.get("picks") or {})
        status = f"🎮 In progress · picks locked: {locked}/2"
    return f"• {player_a} vs {player_b} — {status}"


async def _bracket_timeout(context, mid, bracket_round_idx):
    """Wait for a maximum bracket duration, then forcibly resolve incomplete duels."""
    try:
        # Strict 60 seconds to finish all duels in the current bracket
        await asyncio.sleep(60.0)
    except asyncio.CancelledError:
        return

    rt = runtime.get(mid)
    advance_now = False

    async with rt["lock"]:
        match = await get_match(mid)
        if not match or match.get("stage") != "final" or match.get("game", {}).get("round_finished"):
            return
        
        game = match["game"]
        # If the game naturally advanced to the next bracket, do nothing
        if game.get("bracket_round") != bracket_round_idx:
            return

        incomplete_duels = False
        for duel in game.get("duels", []):
            if duel["winner"] is None:
                incomplete_duels = True
                winner_uid, loser_uid = _timeout_winner(duel)

                duel["winner"] = int(winner_uid)
                game["winners"].append(int(winner_uid))

                loser_player = get_player(match, int(loser_uid))
                if loser_player:
                    loser_player["alive"] = False
                    dm_mid = loser_player.get("dm_mid")
                    if dm_mid:
                        asyncio.create_task(
                            safe_edit(context.bot, int(loser_uid), dm_mid, "💀 <b>TIME OUT!</b> You failed to act and were eliminated.", reply_markup=ui.kb([]))
                        )

                winner_player = get_player(match, int(winner_uid))
                if winner_player:
                    dm_mid = winner_player.get("dm_mid")
                    if dm_mid:
                        asyncio.create_task(
                            safe_edit(context.bot, int(winner_uid), dm_mid, "⏳ <b>Opponent timed out.</b> You advance by default.", reply_markup=ui.kb([]))
                        )

        if incomplete_duels:
            game["last_event"] = "Time expired. Incomplete duels were resolved automatically."
            await save_match(match)
            advance_now = True

    if advance_now:
        await _refresh_group(context, mid)
        await _advance_bracket(context, mid)

        

def _final_details(match, seconds_left=None):
    game = match.get("game") or {}
    duels = game.get("duels") or []
    contenders = game.get("contenders") or []
    bracket_round = int(game.get("bracket_round", 1))
    if duels:
        live_contenders = sum(2 if duel.get("winner") is None else 1 for duel in duels) + len(game.get("byes") or [])
    else:
        live_contenders = len(contenders)
    lines = []
    if seconds_left is not None:
        lines.append(ui.round_waiting_panel(
            "final", seconds_left, total_seconds=PREPARATION_SECONDS,
        ))
        lines.append("")
    lines.extend([
        f"⚔️ <b>Bracket round:</b> {bracket_round}",
        f"👥 <b>Players in bracket:</b> {live_contenders}  ·  "
        f"✅ <b>Duels decided:</b> {sum(1 for duel in duels if duel.get('winner') is not None)}/{len(duels)}",
    ])
    lines.extend(_duel_status_line(match, duel) for duel in duels[:10])
    if len(duels) > 10:
        lines.append(f"…and {len(duels) - 10} more duels")
    for uid in game.get("byes") or []:
        lines.append(f"🎟 {html.escape(pname(match, uid))} advances with a bye")
    lines.extend([
        "",
        "<b>Combat rules</b>",
        "⚔️ Offensive beats 🛡️ Defensive",
        "🛡️ Defensive beats 🃏 Trickster",
        "🃏 Trickster beats ⚔️ Offensive",
        f"❤️ {config.FINAL_HP} HP each · {config.FINAL_DAMAGE} damage per lost exchange",
    ])
    return "\n".join(lines)

async def _refresh_group(context, mid, event=None, finished=False):
    rt = runtime.get(mid)
    async with rt["lock"]:
        match = await get_match(mid)
        if not match or match.get("stage") != "final":
            return
        game = match.get("game") or {}
        if event:
            game["last_event"] = event
            await save_match(match)
    await ui.refresh_round_announcement(
        context.bot, match, "final", details=_final_details(match),
        event=game.get("last_event"), finished=finished,
    )
    if finished:
        await refresh_lobby_scoreboard(context, mid, force=True)


async def begin(context, mid):
    match = await get_match(mid)
    if not match:
        return
    contenders = [player["id"] for player in alive(match)]
    random.shuffle(contenders)
    for player in alive(match):
        player.pop("dm_mid", None)
    match["game"] = {
        "contenders": contenders,
        "duels": [],
        "winners": [],
        "bracket_round": 0,
        "byes": [],
        "last_event": None,
        "round_finished": False,
    }
    await ui.send_round_announcement(
        context.bot, match, "final",
        details=_final_details(match, PREPARATION_SECONDS),
    )
    await save_match(match)

    runtime.add_task(mid, asyncio.create_task(_run_round(context, mid)))


async def _run_round(context, mid):
    """30s prep then start the first bracket."""
    try:
        for delay, shown in [ (5, 10), (5, 5)]:
            await asyncio.sleep(delay)
            latest = await get_match(mid)
            if not latest or latest.get("stage") != "final":
                return
            await ui.refresh_round_announcement(
                context.bot, latest, "final",
                details=_final_details(latest, shown),
            )
        await asyncio.sleep(5)

        latest = await get_match(mid)
        if not latest or latest.get("stage") != "final":
            return
        await _start_bracket_round(context, mid)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("Final run_round crashed")


async def _start_bracket_round(context, mid):
    rt = runtime.get(mid)
    async with rt["lock"]:
        match = await get_match(mid)
        if not match or match.get("stage") != "final":
            return
        game = match["game"]
        ids = list(game["contenders"])
        random.shuffle(ids)
        game["bracket_round"] = int(game.get("bracket_round", 0)) + 1
        game["duels"], game["winners"], game["byes"] = [], [], []
        if len(ids) % 2 == 1:
            bye = ids.pop()
            game["winners"].append(bye)
            game["byes"].append(bye)
            game["last_event"] = f"{pname(match, bye)} receives a bye in bracket round {game['bracket_round']}."
        while ids:
            a, b = ids.pop(), ids.pop()
            game["duels"].append({
                "a": a, "b": b,
                "hp": {str(a): config.FINAL_HP, str(b): config.FINAL_HP},
                "picks": {}, "winner": None,
            })
        await save_match(match)
        duels_empty = not game["duels"]
        winners_count = len(game["winners"])
        bracket_round_idx = game["bracket_round"]

    await _refresh_group(context, mid)
    if duels_empty and winners_count == 1:
        await _advance_bracket(context, mid)
        return

    # Start the timeout task specifically for this bracket round
    # We allocate 60 seconds per bracket round
    runtime.add_task(mid, asyncio.create_task(_bracket_timeout(context, mid, bracket_round_idx)))

    match = await get_match(mid)
    for index in range(len(match["game"]["duels"])):
        await _prompt_duel(context, mid, index, intro=True)

def _duel_prompt_text(match, duel, uid):
    """Build the private duel prompt for one participant."""
    opponent = duel["b"] if uid == duel["a"] else duel["a"]
    own_hp = duel["hp"][str(uid)]
    opponent_hp = duel["hp"][str(opponent)]
    return (
        f"🦑 <b>DUEL</b> vs {html.escape(pname(match, opponent))}\n\n"
        f"You  {ui.hp_bar(own_hp)} {own_hp} HP\n"
        f"Foe  {ui.hp_bar(opponent_hp)} {opponent_hp} HP\n\n"
        "Choose your move. Your choice stays hidden until both players select."
    )

async def _prompt_duel(context, mid, idx, intro=False):
    rt = runtime.get(mid)
    async with rt["lock"]:
        match = await get_match(mid)
        if not match or match.get("stage") != "final":
            return
        duel = match["game"]["duels"][idx]
        updates_needed = []
        for uid in (duel["a"], duel["b"]):
            player = get_player(match, uid)
            updates_needed.append({
                "uid": uid,
                "text": _duel_prompt_text(match, duel, uid),
                "dm_mid": player.get("dm_mid") if player else None,
            })

    for info in updates_needed:
        try:
            if not info["dm_mid"]:
                message = await context.bot.send_message(
                    info["uid"], info["text"], reply_markup=ui.final_keyboard(mid), parse_mode="HTML"
                )
                async with rt["lock"]:
                    match = await get_match(mid)
                    get_player(match, info["uid"])["dm_mid"] = message.message_id
                    await save_match(match)
            else:
                await safe_edit(context.bot, info["uid"], info["dm_mid"], info["text"], reply_markup=ui.final_keyboard(mid))
        except Exception:
            pass


async def on_pick(update, context):
    q = update.callback_query
    _, mid, choice = q.data.split(":")
    if choice not in LABEL:
        await safe_answer(q, "Unknown move.", True)
        return
    uid = q.from_user.id
    rt = runtime.get(mid)

    error_msg = None
    alert = False
    picks_count = 0
    idx = None

    async with rt["lock"]:
        match = await get_match(mid)
        if not match or match.get("stage") != "final":
            error_msg = ""
        else:
            game = match["game"]
            idx = next((index for index, duel in enumerate(game["duels"])
                        if duel["winner"] is None and uid in (duel["a"], duel["b"])), None)
            if idx is None:
                error_msg, alert = "No active duel.", True
            else:
                duel = game["duels"][idx]
                if str(uid) in duel["picks"]:
                    error_msg = "Move locked in!"
                else:
                    duel["picks"][str(uid)] = choice
                    await save_match(match)
                    picks_count = len(duel["picks"])

    if error_msg is not None:
        await safe_answer(q, error_msg, alert)
        return

    await safe_answer(q, f"You chose {LABEL[choice]}")
    await _refresh_group(context, mid)
    if picks_count == 2:
        await _resolve_exchange(context, mid, idx)


def _apply_exchange(match, duel):
    """Apply both locked choices and return the exchange outcome."""
    game = match["game"]
    player_a, player_b = duel["a"], duel["b"]
    key_a, key_b = str(player_a), str(player_b)
    pick_a, pick_b = duel["picks"][key_a], duel["picks"][key_b]

    if pick_a == pick_b:
        event = f"Both duelists chose {LABEL[pick_a]}; no damage dealt."
    elif BEATS[pick_a] == pick_b:
        duel["hp"][key_b] -= config.FINAL_DAMAGE
        event = (
            f"{pname(match, player_a)}'s {LABEL[pick_a]} beats {LABEL[pick_b]}; "
            f"{pname(match, player_b)} takes {config.FINAL_DAMAGE} damage."
        )
    else:
        duel["hp"][key_a] -= config.FINAL_DAMAGE
        event = (
            f"{pname(match, player_b)}'s {LABEL[pick_b]} beats {LABEL[pick_a]}; "
            f"{pname(match, player_a)} takes {config.FINAL_DAMAGE} damage."
        )
    duel["picks"] = {}

    hp_a, hp_b = duel["hp"][key_a], duel["hp"][key_b]
    if hp_a <= 0 or hp_b <= 0:
        loser = player_a if hp_a <= 0 else player_b
        winner = player_b if hp_a <= 0 else player_a
        duel["winner"] = winner
        loser_player = get_player(match, loser)
        winner_player = get_player(match, winner)
        if loser_player:
            loser_player["alive"] = False
        if winner not in game["winners"]:
            game["winners"].append(winner)
        game["last_event"] = (
            f"{event} {pname(match, winner)} wins the duel; "
            f"{pname(match, loser)} is eliminated."
        )
        return {
            "event": game["last_event"],
            "loser": loser,
            "winner": winner,
            "loser_dm": loser_player.get("dm_mid") if loser_player else None,
            "winner_dm": winner_player.get("dm_mid") if winner_player else None,
            "advance_now": all(item["winner"] is not None for item in game["duels"]),
        }

    game["last_event"] = event
    return {"event": event, "loser": None, "winner": None, "advance_now": False}


async def _resolve_exchange(context, mid, idx):
    rt = runtime.get(mid)
    async with rt["lock"]:
        match = await get_match(mid)
        if not match or match.get("stage") != "final":
            return

        game = match["game"]
        duel = game["duels"][idx]
        if len(duel["picks"]) != 2:
            return

        outcome = _apply_exchange(match, duel)
        await save_match(match)

    if outcome["winner"] is not None:
        if outcome.get("loser_dm"):
            await safe_edit(
                context.bot, outcome["loser"], outcome["loser_dm"],
                "💀 You lost the duel. <b>ELIMINATED.</b>", reply_markup=ui.kb([]),
            )
        if outcome.get("winner_dm"):
            await safe_edit(
                context.bot, outcome["winner"], outcome["winner_dm"],
                "🎉 You won the duel! Advancing to the next bracket.", reply_markup=ui.kb([]),
            )
        await _refresh_group(context, mid)
        if outcome["advance_now"]:
            await _advance_bracket(context, mid)
        return

    await _refresh_group(context, mid)
    await _prompt_duel(context, mid, idx, intro=False)


async def _advance_bracket(context, mid):
    rt = runtime.get(mid)
    is_champion = False
    champion_id = None
    contenders = []

    async with rt["lock"]:
        match = await get_match(mid)
        if not match or match.get("stage") != "final":
            return
        game = match["game"]
        game["contenders"] = list(game["winners"])
        contenders = list(game["contenders"])

        if len(game["contenders"]) == 1:
            champion_id = game["contenders"][0]
            game["round_finished"] = True
            game["last_event"] = f"{pname(match, champion_id)} is the final survivor."
            is_champion = True
        else:
            game["last_event"] = f"{len(game['contenders'])} players remain. Enter the next bracket before the timer expires."
        await save_match(match)

    if is_champion:
        await ui.refresh_round_announcement(
            context.bot, match, "final", details=_final_details(match),
            event=match["game"]["last_event"], finished=True,
        )
        await crown(context, mid, champion_id)
    else:
        await open_final_bracket_gate(context, mid, contenders)