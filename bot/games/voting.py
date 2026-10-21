"""Game 5 — Conditional elimination vote."""
import asyncio
import logging
import random
from collections import Counter
import html

from .. import config, runtime, ui
from ..manager import safe_edit, safe_answer, game_finished, refresh_lobby_scoreboard
from ..repository import get_match, save_match, alive, get_player, pname

log = logging.getLogger(__name__)

PREPARATION_SECONDS = 15


def _details(match, seconds_left=None):
    game = match.get("game") or {}
    votes = game.get("votes") or {}
    eligible = int(game.get("eligible_count", sum(1 for player in match.get("players", []) if player.get("alive"))))
    lines = []
    if seconds_left is not None:
        lines.append(ui.round_waiting_panel(
            "vote", seconds_left, total_seconds=PREPARATION_SECONDS,
        ))
        lines.append("")
    lines.append(f"🗳 <b>Votes submitted:</b> {len(votes)}/{eligible}")
    lines.append("<i>Ballots are private. The player with the most votes is eliminated; ties are random.</i>")
    lines.append(f"⏱ <b>Time limit:</b> {config.VOTE_SECONDS}s")
    return "\n".join(lines)


async def _refresh(context, mid, event=None, finished=False):
    rt = runtime.get(mid)
    async with rt["lock"]:
        match = await get_match(mid)
        if not match or match.get("stage") != "vote":
            return
        game = match.get("game") or {}
        if event:
            game["last_event"] = event
            await save_match(match)
    await ui.refresh_round_announcement(
        context.bot, match, "vote", details=_details(match),
        event=game.get("last_event"), finished=finished,
    )
    # Scoreboard update optimization: only refresh it when the round finishes
    if finished:
        await refresh_lobby_scoreboard(context, mid, force=True)


async def begin(context, mid):
    match = await get_match(mid)
    if not match:
        return
    match["game"] = {
        "votes": {}, "resolved": False, "last_event": None,
        "round_finished": False, "eligible_count": len(alive(match)),
    }
    await ui.send_round_announcement(
        context.bot, match, "vote",
        details=_details(match, PREPARATION_SECONDS),
    )
    await save_match(match)

    runtime.add_task(mid, asyncio.create_task(_run_round(context, mid)))


async def _run_round(context, mid):
    """30s prep then send voting DMs and start timeout."""
    try:
        for delay, shown in [(10, 20), (10, 10), (5, 5)]:
            await asyncio.sleep(delay)
            latest = await get_match(mid)
            if not latest or latest.get("stage") != "vote":
                return
            await ui.refresh_round_announcement(
                context.bot, latest, "vote",
                details=_details(latest, shown),
            )
        await asyncio.sleep(5)

        latest = await get_match(mid)
        if not latest or latest.get("stage") != "vote":
            return
        for player in alive(latest):
            try:
                message = await context.bot.send_message(
                    player["id"],
                    "🗳 <b>Vote to eliminate one player:</b>",
                    reply_markup=ui.vote_keyboard(latest, mid, player["id"]),
                    parse_mode="HTML",
                )
                player["dm_mid"] = message.message_id
            except Exception:
                pass
        await save_match(latest)
        await _refresh(context, mid)
        runtime.add_task(mid, asyncio.create_task(_timeout(context, mid)))
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("Voting run_round crashed")


async def on_vote(update, context):
    q = update.callback_query
    _, mid, target = q.data.split(":")
    uid = str(q.from_user.id)
    rt = runtime.get(mid)

    error_msg = None
    alert = False
    voter_dm_mid = None
    trigger_resolve = False

    async with rt["lock"]:
        match = await get_match(mid)
        if not match or match["stage"] != "vote":
            error_msg = ""
        else:
            game = match["game"]
            if uid in game["votes"]:
                error_msg, alert = "You already voted!", True
            else:
                voter = get_player(match, q.from_user.id)
                if not voter or not voter.get("alive"):
                    error_msg, alert = "You are not eligible to vote.", True
                elif int(target) == q.from_user.id or not any(p.get("id") == int(target) and p.get("alive") for p in match.get("players", [])):
                    error_msg, alert = "Choose an eligible player.", True
                else:
                    game["votes"][uid] = int(target)
                    await save_match(match)
                    voter_dm_mid = voter.get("dm_mid")
                    if len(game["votes"]) >= len(alive(match)):
                        trigger_resolve = True

    if error_msg is not None:
        await safe_answer(q, error_msg, alert)
        return

    await safe_answer(q, "Vote registered 🗳")
    if voter_dm_mid:
        await safe_edit(
            context.bot, 
            q.from_user.id, 
            voter_dm_mid, 
            "✅ <b>Vote registered.</b> Waiting for the others…",
            reply_markup=ui.kb([])  
        )
        
    await _refresh(context, mid)

    if trigger_resolve:
        await _resolve(context, mid)


async def _timeout(context, mid):
    try:
        await asyncio.sleep(config.VOTE_SECONDS)
    except asyncio.CancelledError:
        return
    await _resolve(context, mid)


def _select_victim(players, votes):
    """Select the most-voted player, breaking ties randomly."""
    tally = Counter(votes.values())
    if not tally:
        return random.choice([player["id"] for player in players])

    highest_vote_count = max(tally.values())
    tied_players = [
        user_id
        for user_id, count in tally.items()
        if count == highest_vote_count
    ]
    return random.choice(tied_players)


async def _resolve(context, mid):
    rt = runtime.get(mid)
    survivors = []
    event_text = ""
    is_empty = False

    async with rt["lock"]:
        match = await get_match(mid)
        if not match or match.get("stage") != "vote" or match["game"].get("resolved"):
            return
        game = match["game"]
        players = alive(match)

        if not players:
            game["resolved"] = True
            game["round_finished"] = True
            await save_match(match)
            is_empty = True
        else:
            game["resolved"] = True
            victim = _select_victim(players, game["votes"])

            victim_player = get_player(match, victim)
            if victim_player:
                victim_player["alive"] = False
            game["round_finished"] = True
            game["last_event"] = f"Voting concluded. {pname(match, victim)} was eliminated."
            event_text = game["last_event"]
            await save_match(match)
            survivors = [p["id"] for p in players if p["id"] != victim]

    if is_empty:
        await _refresh(context, mid, event="No eligible players remained.", finished=True)
        runtime.cancel_tasks(mid)
        await game_finished(context, mid, [], "Elimination Vote")
        return

    await _refresh(context, mid, event=event_text, finished=True)
    runtime.cancel_tasks(mid)
    await game_finished(context, mid, survivors, "Elimination Vote")