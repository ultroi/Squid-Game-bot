"""Game 6 — Squid Game final."""
import asyncio
import html
import logging
import random

from .. import config, runtime, ui
from ..manager import safe_edit, safe_answer, crown, open_final_bracket_gate, refresh_lobby_scoreboard
from ..repository import get_match, save_match, alive, get_player, pname

log = logging.getLogger(__name__)

PREPARATION_SECONDS = 30

BEATS = {"offense": "defense", "defense": "trickster", "trickster": "offense"}
LABEL = {"offense": "⚔️ Offensive", "defense": "🛡️ Defensive", "trickster": "🃏 Trickster"}


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
    if seconds_left is not None and seconds_left > 0:
        lines.append(f"⏳ <b>First duel opens in {seconds_left}s…</b>")
        lines.append("")
    lines.extend([
        f"⚔️ <b>Bracket round:</b> {bracket_round}",
        f"👥 <b>Players in bracket:</b> {live_contenders}  ·  "
        f"✅ <b>Duels decided:</b> {sum(1 for duel in duels if duel.get('winner') is not None)}/{len(duels)}",
    ])
    for duel in duels[:10]:
        a = html.escape(pname(match, duel["a"]))
        b = html.escape(pname(match, duel["b"]))
        if duel.get("winner") is not None:
            state = f"🏅 {html.escape(pname(match, duel['winner']))} advances"
        else:
            locked = len(duel.get("picks") or {})
            state = f"🎮 In progress · picks locked: {locked}/2"
        lines.append(f"• {a} vs {b} — {state}")
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
    await refresh_lobby_scoreboard(context, mid)


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
        for delay, shown in [(10, 20), (10, 10), (5, 5)]:
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

    await _refresh_group(context, mid)
    if duels_empty and winners_count == 1:
        await _advance_bracket(context, mid)
        return

    match = await get_match(mid)
    for index in range(len(match["game"]["duels"])):
        await _prompt_duel(context, mid, index, intro=True)


async def _prompt_duel(context, mid, idx, intro=False):
    rt = runtime.get(mid)
    async with rt["lock"]:
        match = await get_match(mid)
        if not match or match.get("stage") != "final":
            return
        duel = match["game"]["duels"][idx]
        updates_needed = []
        for uid in (duel["a"], duel["b"]):
            foe = duel["b"] if uid == duel["a"] else duel["a"]
            text = (
                f"🦑 <b>DUEL</b> vs {html.escape(pname(match, foe))}\n\n"
                f"You  {ui.hp_bar(duel['hp'][str(uid)])} {duel['hp'][str(uid)]} HP\n"
                f"Foe  {ui.hp_bar(duel['hp'][str(foe)])} {duel['hp'][str(foe)]} HP\n\n"
                "Choose your move. Your choice stays hidden until both players select."
            )
            player = get_player(match, uid)
            updates_needed.append({"uid": uid, "text": text, "dm_mid": player.get("dm_mid")})

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


async def _resolve_exchange(context, mid, idx):
    rt = runtime.get(mid)
    loser = None
    winner = None
    loser_dm = None
    winner_dm = None
    advance_now = False

    async with rt["lock"]:
        match = await get_match(mid)
        if not match or match.get("stage") != "final":
            return
        game = match["game"]
        duel = game["duels"][idx]

        if len(duel["picks"]) != 2:
            return

        a, b = str(duel["a"]), str(duel["b"])
        pick_a, pick_b = duel["picks"][a], duel["picks"][b]

        if pick_a == pick_b:
            event = f"Both duelists chose {LABEL[pick_a]}; no damage dealt."
        elif BEATS[pick_a] == pick_b:
            duel["hp"][b] -= config.FINAL_DAMAGE
            event = f"{pname(match, duel['a'])}'s {LABEL[pick_a]} beats {LABEL[pick_b]}; {pname(match, duel['b'])} takes {config.FINAL_DAMAGE} damage."
        else:
            duel["hp"][a] -= config.FINAL_DAMAGE
            event = f"{pname(match, duel['b'])}'s {LABEL[pick_b]} beats {LABEL[pick_a]}; {pname(match, duel['a'])} takes {config.FINAL_DAMAGE} damage."
        duel["picks"] = {}

        if duel["hp"][a] <= 0 or duel["hp"][b] <= 0:
            loser = duel["a"] if duel["hp"][a] <= 0 else duel["b"]
            winner = duel["b"] if duel["hp"][a] <= 0 else duel["a"]
            duel["winner"] = winner

            loser_player = get_player(match, loser)
            if loser_player:
                loser_player["alive"] = False
                loser_dm = loser_player.get("dm_mid")

            if winner not in game["winners"]:
                game["winners"].append(winner)

            winner_player = get_player(match, winner)
            if winner_player:
                winner_dm = winner_player.get("dm_mid")

            game["last_event"] = f"{event} {pname(match, winner)} wins the duel; {pname(match, loser)} is eliminated."
            await save_match(match)

            if all(item["winner"] is not None for item in game["duels"]):
                advance_now = True
        else:
            game["last_event"] = event
            await save_match(match)

    if loser is not None and winner is not None:
        if loser_dm:
            await safe_edit(context.bot, loser, loser_dm, "💀 You lost the duel. <b>ELIMINATED.</b>")
        if winner_dm:
            await safe_edit(context.bot, winner, winner_dm, "🎉 You won the duel! Advancing to the next bracket.")

        await _refresh_group(context, mid)
        if advance_now:
            await _advance_bracket(context, mid)
    else:
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