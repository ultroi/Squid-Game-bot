"""Game 4 — Glass Bridge (hopscotch)."""
import asyncio
import html
import logging
import random

from .. import config, runtime, ui
from ..manager import safe_edit, safe_answer, game_finished, refresh_lobby_scoreboard
from ..repository import get_match, save_match, alive, get_player, pname

log = logging.getLogger(__name__)

PREPARATION_SECONDS = 30


async def begin(context, mid):
    match = await get_match(mid)
    if not match:
        return

    queue = [p["id"] for p in alive(match)]
    random.shuffle(queue)
    match["game"] = {
        "tiles": [random.randint(0, 1) for _ in range(config.BRIDGE_LENGTH)],
        "progress": -1,
        "broken": [],
        "queue": queue,
        "idx": 0,
        "crossed": [],
        "last_event": None,
    }
    board = await ui.send_round_announcement(
        context.bot,
        match,
        "bridge",
        details=_preparation_details(match, PREPARATION_SECONDS),
    )
    match["board_message_id"] = board.message_id
    await save_match(match)

    runtime.add_task(mid, asyncio.create_task(_run_round(context, mid)))


async def _run_round(context, mid):
    """30s prep then start bridge."""
    try:
        for delay, shown in [(10, 20), (10, 10), (5, 5)]:
            await asyncio.sleep(delay)
            latest = await get_match(mid)
            if not latest or latest.get("stage") != "bridge":
                return
            await ui.refresh_round_announcement(
                context.bot,
                latest,
                "bridge",
                details=_preparation_details(latest, shown),
            )
        await asyncio.sleep(5)

        latest = await get_match(mid)
        if not latest or latest.get("stage") != "bridge":
            return
        runtime.add_task(mid, asyncio.create_task(_timeout(context, mid)))
        if latest["game"].get("queue"):
            await _prompt(context, mid)
        else:
            await _finish(context, mid)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("Bridge run_round crashed")


def _preparation_details(match, seconds_left):
    """Combine the shared preparation panel with bridge-specific progress."""
    return "\n".join((
        ui.round_waiting_panel(
            "bridge", seconds_left, total_seconds=PREPARATION_SECONDS,
        ),
        "",
        f"🌉 <b>Bridge progress</b>\n{_bridge_details(match)}",
        f"⏱ <b>Play time:</b> {config.BRIDGE_SECONDS}s",
    ))

def _bridge_details(match):
    g = match.get("game") or {}
    queue = g.get("queue") or []
    index = int(g.get("idx", 0))
    current = pname(match, queue[index]) if index < len(queue) else "—"
    grid = html.escape(ui.bridge_grid(g))
    step = min(config.BRIDGE_LENGTH, max(0, int(g.get("progress", -1)) + 2))
    crossed = len(g.get("crossed", []))
    return (
        f"{grid}\n\n"
        f"🧍 <b>Crossing:</b> {html.escape(str(current))}\n"
        f"📍 <b>Step:</b> {step}/{config.BRIDGE_LENGTH}  ·  ✅ <b>Crossed:</b> {crossed}"
    )


async def _refresh(context, mid, event=None, finished=False):
    rt = runtime.get(mid)
    async with rt["lock"]:
        match = await get_match(mid)
        if not match or (match.get("stage") != "bridge" and not finished):
            return
        g = match.get("game") or {}
        if event:
            g["last_event"] = event
            await save_match(match)
    details = (
        f"🌉 <b>Bridge progress</b>\n{_bridge_details(match)}\n"
        f"⏱ Total time: <b>{config.BRIDGE_SECONDS}s</b>"
    )
    await ui.refresh_round_announcement(
        context.bot, match, "bridge", details=details,
        event=g.get("last_event"), finished=finished,
    )
    await refresh_lobby_scoreboard(context, mid)


async def _prompt(context, mid):
    rt = runtime.get(mid)
    async with rt["lock"]:
        match = await get_match(mid)
        if not match or match.get("stage") != "bridge":
            return
        g = match["game"]
        if g["idx"] >= len(g["queue"]):
            trigger_finish = True
            uid = None
        else:
            trigger_finish = False
            uid = g["queue"][g["idx"]]
            p = get_player(match, uid)

    if trigger_finish:
        await _finish(context, mid)
        return

    if not p:
        await _next(context, mid)
        return

    try:
        m = await context.bot.send_message(
            uid,
            f"🌉 <b>GLASS BRIDGE — your turn!</b>\n\n"
            f"Step <b>{g['progress'] + 2}/{config.BRIDGE_LENGTH}</b>\n"
            "One panel is tempered ✅, the other shatters 💥. Choose:",
            reply_markup=ui.bridge_keyboard(mid),
            parse_mode="HTML",
        )
        async with rt["lock"]:
            match = await get_match(mid)
            get_player(match, uid)["dm_mid"] = m.message_id
            await save_match(match)
    except Exception:
        async with rt["lock"]:
            match = await get_match(mid)
            p = get_player(match, uid)
            if p:
                p["alive"] = False
            match["game"]["last_event"] = f"{pname(match, uid)} could not be reached and was eliminated."
            await save_match(match)
        await _refresh(context, mid)
        await _next(context, mid)


def _apply_pick(match, uid, side):
    """Validate and apply one bridge choice while the match lock is held."""
    result = {
        "error_msg": None,
        "alert": False,
        "result_kind": None,
        "row": -1,
        "dm_mid": None,
    }
    if not match or match.get("stage") != "bridge":
        result["error_msg"] = ""
        return result

    game = match["game"]
    queue = game["queue"]
    if game["idx"] >= len(queue) or queue[game["idx"]] != uid:
        result.update(error_msg="Not your turn!", alert=True)
        return result

    player = get_player(match, uid)
    if not player or not player.get("alive"):
        result.update(error_msg="You're eliminated.", alert=True)
        return result

    row = game["progress"] + 1
    result["row"] = row
    result["dm_mid"] = player.get("dm_mid")

    if game["tiles"][row] == side:
        game["progress"] = row
        if row == config.BRIDGE_LENGTH - 1:
            game["crossed"].append(uid)
            game["last_event"] = f"{pname(match, uid)} crossed the bridge safely."
            result["result_kind"] = "win"
        else:
            game["last_event"] = f"{pname(match, uid)} advanced to step {row + 1}."
            result["result_kind"] = "safe"
    else:
        game["broken"].append([row, side])
        player["alive"] = False
        game["last_event"] = (
            f"{pname(match, uid)} fell at step {row + 1} and was eliminated."
        )
        result["result_kind"] = "dead"
    return result


async def _send_pick_feedback(context, query, uid, mid, result):
    """Answer the callback and update the player's private bridge controls."""
    kind = result["result_kind"]
    row = result["row"]
    message_id = result["dm_mid"]

    if kind == "win":
        await safe_answer(query, "✅ Safe!")
        text = "🎉 <b>You crossed the bridge!</b>"
        markup = ui.kb([])
    elif kind == "safe":
        await safe_answer(query, "✅ Safe!")
        text = (
            f"✅ Safe! Step <b>{row + 1}/{config.BRIDGE_LENGTH}</b> — keep going:"
        )
        markup = ui.bridge_keyboard(mid)
    elif kind == "dead":
        await safe_answer(query, "💥 Wrong glass!", True)
        text = "💥💀 <b>The glass shattered. You fell. ELIMINATED.</b>"
        markup = ui.kb([])
    else:
        return

    if message_id:
        await safe_edit(
            context.bot,
            uid,
            message_id,
            text,
            reply_markup=markup,
        )


async def on_pick(update, context):
    query = update.callback_query
    _, mid, raw_side = query.data.split(":")
    side = int(raw_side)
    uid = query.from_user.id
    runtime_state = runtime.get(mid)

    async with runtime_state["lock"]:
        match = await get_match(mid)
        result = _apply_pick(match, uid, side)
        if result["error_msg"] is None and result["result_kind"] is not None:
            await save_match(match)

    if result["error_msg"] is not None:
        await safe_answer(query, result["error_msg"], result["alert"])
        return

    await _send_pick_feedback(context, query, uid, mid, result)
    await _refresh(context, mid)
    if result["result_kind"] in ("win", "dead"):
        await _next(context, mid)


async def _next(context, mid):
    rt = runtime.get(mid)
    async with rt["lock"]:
        match = await get_match(mid)
        if not match or match.get("stage") != "bridge":
            return
        g = match["game"]
        g["idx"] += 1
        await save_match(match)
        idx_status = g["idx"] >= len(g["queue"])

    if idx_status:
        await _finish(context, mid)
    else:
        await _prompt(context, mid)


async def _timeout(context, mid):
    try:
        await asyncio.sleep(config.BRIDGE_SECONDS)
    except asyncio.CancelledError:
        return
    await _finish(context, mid, timed_out=True)


async def _finish(context, mid, timed_out=False):
    rt = runtime.get(mid)
    crossed_list = []

    async with rt["lock"]:
        match = await get_match(mid)
        if not match or match.get("stage") != "bridge" or match.get("game", {}).get("round_finished"):
            return
        match["game"]["round_finished"] = True
        crossed = set(match["game"].get("crossed", []))
        for player in match.get("players", []):
            if player.get("id") not in crossed:
                player["alive"] = False
        match["game"]["last_event"] = (
            "Time expired. Anyone who had not crossed was eliminated."
            if timed_out else "The bridge round is complete."
        )
        await save_match(match)
        crossed_list = list(match["game"]["crossed"])

    runtime.cancel_tasks(mid)
    await _refresh(context, mid, finished=True)
    await game_finished(context, mid, crossed_list, "Glass Bridge")