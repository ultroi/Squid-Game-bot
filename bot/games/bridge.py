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
        details=(
            f"⏳ <b>Round starts in {PREPARATION_SECONDS}s…</b>\n\n"
            f"🌉 <b>Bridge progress</b>\n{_bridge_details(match)}\n"
            f"⏱ Total time: <b>{config.BRIDGE_SECONDS}s</b>\n"
            "<i>Turn prompts and left/right choices arrive privately.</i>"
        ),
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
                context.bot, latest, "bridge",
                details=(
                    f"⏳ <b>Round starts in {shown}s…</b>\n\n"
                    f"🌉 <b>Bridge progress</b>\n{_bridge_details(latest)}\n"
                    f"⏱ Total time: <b>{config.BRIDGE_SECONDS}s</b>"
                ),
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


async def on_pick(update, context):
    q = update.callback_query
    _, mid, side = q.data.split(":")
    side = int(side)
    uid = q.from_user.id
    rt = runtime.get(mid)

    error_msg = None
    alert = False
    result_kind = None
    row = -1
    dm_mid = None

    async with rt["lock"]:
        match = await get_match(mid)
        if not match or match["stage"] != "bridge":
            error_msg = ""
        else:
            g = match["game"]
            if g["idx"] >= len(g["queue"]) or g["queue"][g["idx"]] != uid:
                error_msg, alert = "Not your turn!", True
            else:
                p = get_player(match, uid)
                if not p or not p.get("alive"):
                    error_msg, alert = "You're eliminated.", True
                else:
                    dm_mid = p.get("dm_mid")
                    row = g["progress"] + 1
                    if g["tiles"][row] == side:
                        g["progress"] = row
                        if row == config.BRIDGE_LENGTH - 1:
                            g["crossed"].append(uid)
                            g["last_event"] = f"{pname(match, uid)} crossed the bridge safely."
                            result_kind = "win"
                        else:
                            g["last_event"] = f"{pname(match, uid)} advanced to step {row + 1}."
                            result_kind = "safe"
                    else:
                        g["broken"].append([row, side])
                        p["alive"] = False
                        g["last_event"] = f"{pname(match, uid)} fell at step {row + 1} and was eliminated."
                        result_kind = "dead"
                    await save_match(match)

    if error_msg is not None:
        await safe_answer(q, error_msg, alert)
        return

    if result_kind == "win":
        await safe_answer(q, "✅ Safe!")
        if dm_mid:
            await safe_edit(
                context.bot, uid, dm_mid, 
                "🎉 <b>You crossed the bridge!</b>", 
                reply_markup=ui.kb([])
            )
    elif result_kind == "safe":
        await safe_answer(q, "✅ Safe!")
        if dm_mid:
            await safe_edit(
                context.bot, uid, dm_mid,
                f"✅ Safe! Step <b>{row + 1}/{config.BRIDGE_LENGTH}</b> — keep going:",
                reply_markup=ui.bridge_keyboard(mid),
            )
    elif result_kind == "dead":
        await safe_answer(q, "💥 Wrong glass!", True)
        if dm_mid:
            await safe_edit(
                context.bot, uid, dm_mid, 
                "💥💀 <b>The glass shattered. You fell. ELIMINATED.</b>", 
                reply_markup=ui.kb([])
            )

    await _refresh(context, mid)
    if result_kind in ("win", "dead"):
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