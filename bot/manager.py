import asyncio
import html
import logging
import time

from telegram.constants import ParseMode
from telegram.error import BadRequest, RetryAfter

from . import config, runtime, ui
from .repository import (
    get_match, save_match, alive, get_player, record_game_start,
    record_match_participation, record_round_participation, record_match_result,
    record_match_interruption,
)

log = logging.getLogger(__name__)

# Keep fire-and-forget notification tasks strongly referenced until they finish.
_background_tasks = set()


def _spawn_background_task(coro, *, name=None):
    """Schedule work without blocking the game flow and retain its task reference."""
    task = asyncio.create_task(coro, name=name)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


# ---------- messaging helpers ----------

async def delayed_unpin_board(context, chat_id, message_id, delay=4.0):
    """Match khatam hone ke 4 seconds baad scoreboard/board message ko unpin karega."""
    await asyncio.sleep(delay)
    try:
        await context.bot.unpin_chat_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        log.debug("Could not unpin match board message", exc_info=True)

async def group_send(context, match, text, **kwargs):
    for attempt in range(2):
        try:
            return await context.bot.send_message(
                match["chat_id"], text, parse_mode=ParseMode.HTML, **kwargs
            )
        except RetryAfter as exc:
            if attempt == 0:
                delay = getattr(exc, "retry_after", 1)
                if hasattr(delay, "total_seconds"):
                    delay = delay.total_seconds()
                await asyncio.sleep(max(1.0, float(delay)) + 0.2)
            else:
                raise


async def safe_edit(bot, chat_id, message_id, text, reply_markup=None):
    if not message_id:
        return
    for attempt in range(2):
        try:
            await bot.edit_message_text(
                text=text,
                chat_id=chat_id,
                message_id=message_id,
                reply_markup=reply_markup,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
            return
        except BadRequest as exc:
            if "not modified" not in str(exc).lower():
                log.warning("edit failed: %s", exc)
            return
        except RetryAfter as exc:
            if attempt == 0:
                delay = getattr(exc, "retry_after", 1)
                if hasattr(delay, "total_seconds"):
                    delay = delay.total_seconds()
                await asyncio.sleep(max(1.0, float(delay)) + 0.2)
            else:
                log.warning("edit failed for chat %s message %s (Flood control)", chat_id, message_id)
                return
        except Exception:
            log.exception("edit failed for chat %s message %s", chat_id, message_id)
            return


async def safe_answer(query, text=None, alert=False):
    try:
        await query.answer(text, show_alert=alert)
    except Exception:
        pass


async def _edit_round_card(context, match, text, reply_markup=None):
    """Edit the single group round card; create a replacement if it was deleted."""
    markup = reply_markup if reply_markup is not None else ui.kb([])
    message_id = match.get("round_message_id")
    
    if message_id:
        for attempt in range(2):
            try:
                await context.bot.edit_message_text(
                    chat_id=match["chat_id"],
                    message_id=message_id,
                    text=text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=markup,
                    disable_web_page_preview=True,
                )
                return
            except BadRequest as exc:
                if "message is not modified" in str(exc).lower():
                    return
                log.warning("Could not edit round card %s; creating a replacement: %s", message_id, exc)
                break
            except RetryAfter as exc:
                if attempt == 0:
                    delay = getattr(exc, "retry_after", 1)
                    if hasattr(delay, "total_seconds"):
                        delay = delay.total_seconds()
                    await asyncio.sleep(max(1.0, float(delay)) + 0.2)
                else:
                    log.warning("Could not edit round card %s due to flood control; creating replacement", message_id)
                    break
            except Exception:
                log.exception("Could not edit round card %s; creating a replacement", message_id)
                break

    try:
        message = await group_send(
            context,
            match,
            text,
            reply_markup=markup,
            disable_web_page_preview=True,
        )
        match["round_message_id"] = message.message_id
        match["round_message_stage"] = match.get("stage")
        await save_match(match)
    except Exception:
        log.exception("Could not create replacement round card for match %s", match.get("_id"))


async def _send_next_round_gate(context, match):
    """Post the entry gate as its own message, separate from round announcements."""
    message = await group_send(
        context,
        match,
        ui.next_round_gate_text(match, int(config.ROUND_ENTRY_SECONDS)),
        reply_markup=ui.next_round_keyboard(str(match["_id"])),
        disable_web_page_preview=True,
    )
    match["next_round_message_id"] = message.message_id
    await save_match(match)
    return message


async def _edit_next_round_gate(context, match, text):
    """Refresh only the entry-gate message; never overwrite the round card."""
    message_id = match.get("next_round_message_id")
    if message_id:
        for attempt in range(2):
            try:
                await context.bot.edit_message_text(
                    chat_id=match["chat_id"],
                    message_id=message_id,
                    text=text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=ui.next_round_keyboard(str(match["_id"])),
                    disable_web_page_preview=True,
                )
                return
            except BadRequest as exc:
                err_msg = str(exc).lower()
                if "message is not modified" in err_msg:
                    return
                if "not found" in err_msg:
                    # The message was deleted. Break quietly to trigger the replacement below.
                    break
                log.warning("Could not edit next-round gate %s: %s", message_id, exc)
                break
            except RetryAfter as exc:
                if attempt == 0:
                    delay = getattr(exc, "retry_after", 1)
                    if hasattr(delay, "total_seconds"):
                        delay = delay.total_seconds()
                    await asyncio.sleep(max(1.0, float(delay)) + 0.2)
                else:
                    log.warning("Could not edit next-round gate %s due to flood control", message_id)
            except Exception:
                log.exception("Could not edit next-round gate %s", message_id)
                break

    # If the original gate was deleted manually, recreate it and save its new ID.
    try:
        replacement = await group_send(
            context,
            match,
            text,
            reply_markup=ui.next_round_keyboard(str(match["_id"])),
            disable_web_page_preview=True,
        )
        match["next_round_message_id"] = replacement.message_id
        await save_match(match)
    except Exception:
        log.exception("Could not recreate next-round gate for match %s", match.get("_id"))


async def _delete_group_message(context, chat_id, message_id):
    if not message_id:
        return
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except BadRequest as exc:
        # Telegram may report that the message was already deleted.
        if "message to delete not found" not in str(exc).lower():
            log.debug("Could not delete group message %s: %s", message_id, exc)
    except Exception:
        log.exception("Could not delete group message %s", message_id)


async def refresh_lobby_scoreboard(context, mid, force=False):
    rt = runtime.get(str(mid))
    async with rt.setdefault("lobby_board_lock", asyncio.Lock()):
        now_monotonic = time.monotonic()
        last_update = float(rt.get("lobby_board_last_update", 0))
        if not force and now_monotonic - last_update < 1.25:
            return
        match = await get_match(mid)
        if not match:
            return

        stage = match.get("stage") or "lobby"

        # ---------------- LOBBY PHASE: standard HTML + inline keyboard --------
        if stage == "lobby":
            if not match.get("lobby_message_id"):
                return
            text = ui.render_lobby(match)
            markup = ui.lobby_keyboard(mid, False)
            try:
                await context.bot.edit_message_text(
                    chat_id=match["chat_id"],
                    message_id=match["lobby_message_id"],
                    text=text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=markup,
                    disable_web_page_preview=True,
                )
                rt["lobby_board_last_update"] = time.monotonic()
            except BadRequest as exc:
                if "message is not modified" in str(exc).lower():
                    rt["lobby_board_last_update"] = time.monotonic()
                    return
                log.warning("Lobby scoreboard edit failed: %s", exc)
                rt["lobby_board_last_update"] = time.monotonic()
            except RetryAfter as exc:
                delay = getattr(exc, "retry_after", 1)
                if hasattr(delay, "total_seconds"):
                    delay = delay.total_seconds()
                await asyncio.sleep(max(1.0, float(delay)) + 0.2)
            except Exception:
                log.exception("Lobby scoreboard edit failed (mid=%s)", mid)
            return

        # ---------------- POST-LOBBY PHASE: rich scoreboard ------------------
        rich_html = ui.render_match_scoreboard(match)
        scoreboard_id = match.get("scoreboard_message_id")

        if not scoreboard_id:
            new_id = None
            for attempt in range(2):
                try:
                    result = await ui.send_rich_message(
                        context.bot, match["chat_id"], rich_html
                    )
                    new_id = (
                        result.get("message_id")
                        if isinstance(result, dict)
                        else getattr(result, "message_id", None)
                    )
                    break
                except RetryAfter as exc:
                    if attempt:
                        break
                    delay = getattr(exc, "retry_after", 1)
                    if hasattr(delay, "total_seconds"):
                        delay = delay.total_seconds()
                    await asyncio.sleep(max(1.0, float(delay)) + 0.2)
                except Exception:
                    log.exception(
                        "Could not send rich scoreboard (mid=%s)", mid
                    )
                    break

            if not new_id:
                log.warning(
                    "sendRichMessage returned no message_id (mid=%s)", mid
                )
                rt["lobby_board_last_update"] = time.monotonic()
                return

            old_id = match.get("lobby_message_id")
            match["scoreboard_message_id"] = new_id
            match["lobby_message_id"] = None
            await save_match(match)

            if old_id and old_id != new_id:
                try:
                    await context.bot.delete_message(
                        chat_id=match["chat_id"], message_id=old_id
                    )
                except Exception:
                    # Deleting the old lobby is cosmetic; ignore failures.
                    pass
            try:
                await context.bot.pin_chat_message(
                    chat_id=match["chat_id"],
                    message_id=new_id,
                    disable_notification=True,
                )
            except Exception as exc:
                log.debug("Could not pin scoreboard (mid=%s): %s", mid, exc)

            rt["lobby_board_last_update"] = time.monotonic()
            return

        # Update the existing rich scoreboard.
        try:
            await ui.edit_rich_message(
                context.bot, match["chat_id"], scoreboard_id, rich_html
            )
            rt["lobby_board_last_update"] = time.monotonic()
            return
        except BadRequest as exc:
            if "message is not modified" in str(exc).lower():
                rt["lobby_board_last_update"] = time.monotonic()
                return
            log.warning("Rich scoreboard edit failed (mid=%s): %s", mid, exc)
            match["scoreboard_message_id"] = None
            await save_match(match)
            rt["lobby_board_last_update"] = time.monotonic()
            return
        except RetryAfter as exc:
            delay = getattr(exc, "retry_after", 1)
            if hasattr(delay, "total_seconds"):
                delay = delay.total_seconds()
            await asyncio.sleep(max(1.0, float(delay)) + 0.2)
            try:
                await ui.edit_rich_message(
                    context.bot, match["chat_id"], scoreboard_id, rich_html
                )
                rt["lobby_board_last_update"] = time.monotonic()
            except BadRequest as retry_exc:
                if "message is not modified" not in str(retry_exc).lower():
                    log.warning(
                        "Rich scoreboard retry failed (mid=%s): %s",
                        mid, retry_exc,
                    )
                rt["lobby_board_last_update"] = time.monotonic()
            except Exception as retry_exc:
                log.warning(
                    "Rich scoreboard retry failed (mid=%s): %s",
                    mid, retry_exc,
                )
                rt["lobby_board_last_update"] = time.monotonic()
            return
        except Exception:
            log.exception("Rich scoreboard edit failed (mid=%s)", mid)
            rt["lobby_board_last_update"] = time.monotonic()


# ---------- round flow ----------


def _next_stage(from_stage, entrants_count):
    """Choose the next game based on actual entrants, not prior qualifiers."""
    if from_stage == "lobby":
        return "rlgl"
    if from_stage == "rlgl":
        return "dalgona"
    if from_stage == "dalgona":
        return "marbles"
    if from_stage == "marbles":
        return "bridge"
    if from_stage == "bridge":
        # Vote is conditional on the number that actually enters the next round.
        return "vote" if entrants_count % 2 == 1 else "final"
    if from_stage == "vote":
        return "final"
    return None


def _round_module(stage):
    from .games import rlgl, dalgona, marbles, bridge, voting, final
    return {
        "rlgl": rlgl,
        "dalgona": dalgona,
        "marbles": marbles,
        "bridge": bridge,
        "vote": voting,
        "final": final,
    }.get(stage)


def _append_round_history(match, stage, round_id, qualified_ids, eliminated_ids, completed_at=None):
    """Keep each stage/bracket result permanently distinguishable within a match."""
    history = match.setdefault("round_history", [])
    if any(str(item.get("round_id")) == str(round_id) for item in history):
        return
    history.append({
        "round_id": str(round_id),
        "stage": str(stage),
        "qualified_ids": list(qualified_ids or []),
        "eliminated_ids": list(eliminated_ids or []),
        "completed_at": float(completed_at or time.time()),
        # Durable recovery marker: save the outcome before updating user counters.
        "stats_applied": False,
    })


def _mark_round_stats_applied(match, round_id) -> None:
    for item in match.get("round_history", []):
        if str(item.get("round_id")) == str(round_id):
            item["stats_applied"] = True
            return


async def reconcile_pending_round_statistics(limit: int = 1000) -> int:
    """Replay durable round-history entries left pending by a process/database failure."""
    from .database import matches, match_history

    limit = max(1, min(int(limit), 10000))
    reconciled = 0

    async def reconcile_doc(collection, document, *, is_match: bool) -> int:
        match_id = str(document.get("_id") or document.get("match_id") or "")
        if not match_id:
            return 0
        history = document.get("round_history") or []
        changed = False
        successes = 0
        for item in history:
            if item.get("stats_applied") is not False:
                continue
            try:
                await record_round_participation(
                    match_id, str(item.get("stage") or ""),
                    item.get("qualified_ids") or [], item.get("eliminated_ids") or [],
                    round_id=str(item.get("round_id") or item.get("stage") or ""),
                )
                item["stats_applied"] = True
                changed = True
                successes += 1
            except Exception:
                log.exception(
                    "Could not reconcile round stats (match=%s round=%s)",
                    match_id, item.get("round_id"),
                )
        if changed:
            if is_match:
                document["round_history"] = history
                await save_match(document)
            else:
                await collection.update_one(
                    {"_id": document["_id"]},
                    {"$set": {"round_history": history}},
                )
        return successes

    try:
        cursor = matches.find({"round_history.stats_applied": False}).batch_size(100)
        async for document in cursor:
            try:
                reconciled += await reconcile_doc(matches, document, is_match=True)
            except Exception:
                log.exception("Could not reconcile pending round stats for match %s", document.get("_id"))
            if reconciled >= limit:
                return reconciled

        cursor = match_history.find({"round_history.stats_applied": False}).batch_size(100)
        async for document in cursor:
            try:
                reconciled += await reconcile_doc(match_history, document, is_match=False)
            except Exception:
                log.exception("Could not reconcile pending round stats for history %s", document.get("_id"))
            if reconciled >= limit:
                break
    except Exception:
        log.exception("Could not scan pending round-stat records")
    return reconciled


async def game_finished(context, mid, survivor_ids, title):
    """Serialize round finalization so competing timeout/callback paths cannot
    open multiple gates or send duplicate results for the same round.
    """
    state = runtime.get(str(mid))
    async with state.setdefault("round_finish_lock", asyncio.Lock()):
        await _game_finished_once(context, mid, survivor_ids, title)


async def _game_finished_once(context, mid, survivor_ids, title):
    match = await get_match(mid)
    if not match or match.get("stage") in {"finished", "intermission"}:
        return

    # Ignore duplicate completion callbacks after a gate has already been opened.
    existing_gate = match.get("next_round") or {}
    if existing_gate.get("status") == "open":
        return

    previous_stage = match.get("stage")
    survivor_ids = {str(uid) for uid in (survivor_ids or [])}
    entrants = {str(uid) for uid in (match.get("current_round_player_ids") or [])}
    if not entrants:
        # Compatibility with matches created before the persistent score table existed.
        entrants = {
            str(player.get("id")) for player in match.get("players", [])
            if player.get("alive", True) or str(player.get("id")) in {str(uid) for uid in survivor_ids}
        }
    qualified_ids = []
    eliminated_ids = []
    survivors = []
    newly_eliminated = []
    for player in match.get("players", []):
        uid = str(player.get("id"))
        if uid not in entrants:
            continue
        if str(player.get("id")) in survivor_ids and player.get("alive", True):
            survivors.append(player)
            qualified_ids.append(player.get("id"))
        else:
            eliminated_ids.append(player.get("id"))
            if player.get("alive", True):
                player["alive"] = False
                newly_eliminated.append(player)

    match.setdefault("round_results", {})[previous_stage] = {
        "qualified_ids": qualified_ids,
        "eliminated_ids": eliminated_ids,
        "completed_at": time.time(),
    }
    round_id = (
        f"final:{int((match.get('game') or {}).get('bracket_round', 1))}"
        if previous_stage == "final" else previous_stage
    )
    _append_round_history(
        match, previous_stage, round_id, qualified_ids, eliminated_ids,
        completed_at=match["round_results"][previous_stage]["completed_at"],
    )
    match["round_preparing"] = False
    # Persist qualified/eliminated IDs before touching several user documents. If
    # a write fails, startup reconciliation can replay this pending round event.
    await save_match(match)
    await record_round_participation(
        mid, previous_stage, qualified_ids, eliminated_ids, round_id=round_id
    )
    _mark_round_stats_applied(match, round_id)
    await save_match(match)
    await refresh_lobby_scoreboard(context, mid, force=True)

    # Send result DMs concurrently in the background. Crown/gate handling must not
    # wait for private Telegram API requests to complete.
    if newly_eliminated or len(survivors) > 1:
        async def _send_result_dms():
            async def _send_dm(uid, message_text):
                try:
                    await context.bot.send_message(
                        chat_id=uid,
                        text=message_text,
                        parse_mode=ParseMode.HTML,
                    )
                except Exception:
                    # A failed private notice must not interrupt the match flow.
                    log.debug("Could not send round-result DM to user %s", uid, exc_info=True)

            dm_tasks = [
                _send_dm(
                    player["id"],
                    f"💀 <b>{html.escape(title)}</b> — you were eliminated.",
                )
                for player in newly_eliminated
            ]
            if len(survivors) > 1:
                dm_tasks.extend(
                    _send_dm(
                        player["id"],
                        f"✅ You qualified <b>{html.escape(title)}</b>. "
                        "Return to the group and tap ENTER NEXT ROUND before the timer expires.",
                    )
                    for player in survivors
                )
            if dm_tasks:
                await asyncio.gather(*dm_tasks, return_exceptions=True)

        _spawn_background_task(_send_result_dms(), name=f"round-result-dms:{mid}")

    if not survivors:
        match["stage"] = "finished"
        match["next_round"] = {"status": "closed", "from_stage": previous_stage}
        await record_match_result(
            mid, match.get("players", []), None, match.get("round_results"),
            chat_id=match.get("chat_id"), round_history=match.get("round_history", []),
            reason="no_players_qualified",
        )
        match["stats_finalized"] = True
        await save_match(match)

        scoreboard_id = match.get("scoreboard_message_id")
        if scoreboard_id:
            _spawn_background_task(
                delayed_unpin_board(context, match["chat_id"], scoreboard_id, delay=4.0),
                name=f"unpin-board:{mid}",
            )
            
        game_state = match.get("game") or {}
        if not game_state.get("final_group_result_message_id"):
            result = (match.get("round_results") or {}).get(previous_stage) or {}
            qualified_ids = result.get("qualified_ids") or []
            eliminated_ids = result.get("eliminated_ids") or []
            player_by_id = {
                str(player.get("id")): player
                for player in match.get("players", [])
                if player.get("id") is not None
            }

            # Legacy match records may not have current-round IDs saved.
            if not eliminated_ids:
                fallback_ids = match.get("current_round_player_ids") or []
                if not fallback_ids:
                    game_players = game_state.get("players")
                    if isinstance(game_players, dict):
                        fallback_ids = list(game_players.keys())
                eliminated_ids = fallback_ids

            qualified_names = [
                html.escape(str((player_by_id.get(str(uid)) or {}).get("name") or uid))
                for uid in qualified_ids
            ]
            eliminated_names = [
                html.escape(str((player_by_id.get(str(uid)) or {}).get("name") or uid))
                for uid in eliminated_ids
            ]
            fallback_text = (
                f"🏁 <b>{html.escape(str(title))} — FINAL RESULTS</b>\n\n"
                f"✅ <b>Qualified ({len(qualified_ids)}):</b> "
                f"{', '.join(qualified_names) if qualified_names else 'None'}\n"
                f"💀 <b>Eliminated ({len(eliminated_ids)}):</b> "
                f"{', '.join(eliminated_names) if eliminated_names else 'None'}\n\n"
                "❌ <b>No players qualified.</b> The match is over; no next-round entry will open."
            )
            try:
                result_message = await group_send(
                    context,
                    match,
                    fallback_text,
                        disable_web_page_preview=True,
                )
                if result_message:
                    game_state["final_group_result_message_id"] = result_message.message_id
                    match["game"] = game_state
                    await save_match(match)
            except Exception:
                log.exception(
                    "Could not send zero-survivor result message (match=%s)", mid
                )

        await refresh_lobby_scoreboard(context, mid, force=True)
        runtime.clear(mid)
        return

    # If the game already has one survivor, existing game rules make them champion.
    if len(survivors) == 1:
        await crown(context, mid, survivors[0]["id"])
        return

    now = time.time()
    gate = {
        "status": "open",
        "from_stage": previous_stage,
        "completed_title": title,
        "eligible_ids": [player["id"] for player in survivors],
        "joined_ids": [],
        "opened_at": now,
        "deadline": now + max(1, int(config.ROUND_ENTRY_SECONDS)),
    }
    match["stage"] = "intermission"
    match["next_round"] = gate
    await save_match(match)
    await refresh_lobby_scoreboard(context, mid, force=True)

    match["next_round_message_id"] = None
    await _send_next_round_gate(context, match)
    runtime.add_task(mid, asyncio.create_task(_next_round_entry_timeout(context, mid), name=f"next-round-gate:{mid}"))


async def on_next_round(update, context):
    """Callback handler for ``nr:enter:<match_id>`` group buttons."""
    query = update.callback_query
    parts = (query.data or "").split(":", 2)
    if len(parts) != 3 or parts[0] != "nr" or parts[1] != "enter":
        await safe_answer(query, "Invalid next-round button.", True)
        return
    mid = parts[2]
    rt = runtime.get(mid)

    response = None
    alert = False
    refresh = False
    close_now = False

    async with rt["lock"]:
        match = await get_match(mid)
        gate = (match or {}).get("next_round") or {}
        if not match or match.get("stage") != "intermission" or gate.get("status") != "open":
            response, alert = "Next-round entry is closed.", True
        elif time.time() >= float(gate.get("deadline", 0)):
            response, alert, close_now = "The entry window has closed.", True, True
        elif str(query.from_user.id) not in {str(uid) for uid in gate.get("eligible_ids", [])}:
            response, alert = "Only qualified players can enter this round.", True
        elif str(query.from_user.id) in {str(uid) for uid in gate.get("joined_ids", [])}:
            response = "You're already registered for the next round. ✅"
            close_now = {str(uid) for uid in gate.get("joined_ids", [])} >= {
                str(uid) for uid in gate.get("eligible_ids", [])
            }
        else:
            eligible_by_string = {str(uid): uid for uid in gate.get("eligible_ids", [])}
            gate.setdefault("joined_ids", []).append(
                eligible_by_string.get(str(query.from_user.id), query.from_user.id)
            )
            await save_match(match)
            entered = len(gate["joined_ids"])
            eligible = len(gate.get("eligible_ids", []))
            response = f"Entry confirmed! ✅ ({entered}/{eligible})"
            refresh = True
            close_now = entered >= eligible

    await safe_answer(query, response, alert)
    if close_now:
        await _close_next_round_gate(context, mid, force=True)
    elif refresh:
        await _refresh_next_round_gate(context, mid)


async def _refresh_next_round_gate(context, mid):
    """Serialize countdown edits to the separate next-round entry message."""
    rt = runtime.get(mid)
    async with rt.setdefault("gate_refresh_lock", asyncio.Lock()):
        match = await get_match(mid)
        gate = (match or {}).get("next_round") or {}
        if not match or match.get("stage") != "intermission" or gate.get("status") != "open":
            return
        remaining = max(0, int(float(gate.get("deadline", time.time())) - time.time() + 0.999))
        await _edit_next_round_gate(
            context, match, ui.next_round_gate_text(match, remaining)
        )


async def _next_round_entry_timeout(context, mid):
    """Update the countdown at meaningful checkpoints and close on deadline."""
    checkpoints = {15, 10, 5, 4, 3, 2, 1}
    try:
        while True:
            match = await get_match(mid)
            gate = (match or {}).get("next_round") or {}
            if not match or match.get("stage") != "intermission" or gate.get("status") != "open":
                return
            remaining = max(0, int(float(gate.get("deadline", time.time())) - time.time() + 0.999))
            if remaining <= 0:
                break
            joined = {str(uid) for uid in gate.get("joined_ids", [])}
            eligible = {str(uid) for uid in gate.get("eligible_ids", [])}
            if eligible and joined >= eligible:
                break
            if remaining in checkpoints:
                await _refresh_next_round_gate(context, mid)
            await asyncio.sleep(min(1.0, max(0.05, float(gate.get("deadline", time.time()) - time.time()))))
    except asyncio.CancelledError:
        return
    await _close_next_round_gate(context, mid, force=True)


async def _close_next_round_gate(context, mid, force=False):
    """Close the entry window exactly once and start only opted-in players."""
    rt = runtime.get(mid)
    no_entrants = False
    champion_id = None
    target_stage = None
    entrants_count = 0
    gate_message_id = None
    chat_id_cache = None

    async with rt["lock"]:
        match = await get_match(mid)
        gate = (match or {}).get("next_round") or {}
        if not match or match.get("stage") != "intermission" or gate.get("status") != "open":
            return
            
        gate_message_id = match.get("next_round_message_id")
        chat_id_cache = match.get("chat_id")
        
        # De-duplicate IDs by their string form so legacy documents containing
        # both integer and string representations cannot count one player twice.
        entered_by_id = {str(uid): uid for uid in gate.get("joined_ids", [])}
        entered = list(entered_by_id.values())
        eligible = list(gate.get("eligible_ids", []))
        deadline_passed = time.time() >= float(gate.get("deadline", 0))
        all_entered = {str(uid) for uid in entered} >= {str(uid) for uid in eligible}
        if not force and not deadline_passed and not all_entered:
            return

        entered_set = {str(uid) for uid in entered}
        eligible_set = {str(uid) for uid in eligible}
        for player in match.get("players", []):
            player_id = str(player.get("id"))
            if player_id in eligible_set and player_id not in entered_set:
                player["alive"] = False

        gate["status"] = "starting"
        gate["entered_ids"] = entered
        gate["closed_at"] = time.time()
        entrants_count = len(entered)

        if entrants_count == 0:
            no_entrants = True
            gate["status"] = "closed"
            match["stage"] = "finished"
        elif entrants_count == 1:
            champion_id = entered[0]
            match["next_round_message_id"] = None
        else:
            if gate.get("mode") == "final_bracket":
                target_stage = "final"
                # The prior bracket stored its winners in `contenders`; only users
                # who explicitly entered are allowed into the next bracket.
                match.setdefault("game", {})["contenders"] = entered
                # A Final column represents the current bracket, not every bracket
                # collapsed together, so clear the previous bracket result at restart.
                match.setdefault("round_results", {}).pop("final", None)
            else:
                target_stage = _next_stage(gate.get("from_stage"), entrants_count)
            if target_stage is None:
                no_entrants = True
                gate["status"] = "closed"
                match["stage"] = "finished"
            else:
                gate["target_stage"] = target_stage
                match["next_round_message_id"] = None
                match["stage"] = target_stage
                match["current_round_stage"] = target_stage
                match["current_round_player_ids"] = list(entered)
                match["round_preparing"] = True

        await save_match(match)

    # Cancel any prior round/entry timers; the current task is excluded by runtime.
    runtime.cancel_tasks(mid)

    # A successful entry closes and removes the standalone gate message before
    # the next round's fresh announcement is posted.
    if (target_stage or champion_id is not None) and gate_message_id and chat_id_cache:
        await _delete_group_message(context, chat_id_cache, gate_message_id)

    if no_entrants:
        if match and not match.get("stats_finalized"):
            await record_match_result(
                mid, match.get("players", []), None, match.get("round_results"),
                chat_id=match.get("chat_id"), round_history=match.get("round_history", []),
                reason="no_next_round_entries",
            )
            match["stats_finalized"] = True
            await save_match(match)
        await refresh_lobby_scoreboard(context, mid, force=True)
        closed_text = (
            "🛑 <b>ENTRY WINDOW CLOSED</b>\n\n"
            "No qualified players entered the next round in time.\n"
            "🩸 <b>The game is over.</b> Start a new lobby with /enterthehell."
        )
        if gate_message_id and chat_id_cache:
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id_cache,
                    message_id=gate_message_id,
                    text=closed_text,
                    parse_mode=ParseMode.HTML,
                        disable_web_page_preview=True,
                )
            except Exception:
                log.exception("Could not close entry-gate message %s", gate_message_id)
        else:
            # Fallback if there was no separate gate message
            # Reconstruct enough of the match dict to send the final card
            fallback_match = {"chat_id": chat_id_cache, "_id": mid}
            await _edit_round_card(context, fallback_match, closed_text, reply_markup=ui.kb([]))
        runtime.clear(mid)
        return

    if champion_id is not None:
        await crown(context, mid, champion_id)
        return

    if not target_stage:
        return

    # Reset per-match runtime so stale grid/phase/token state from the previous
    # round cannot leak into the next game's begin().
    runtime.clear(mid)

    match = await get_match(mid)
    if not match:
        return
    await refresh_lobby_scoreboard(context, mid, force=True)
    module = _round_module(target_stage)
    if module is None:
        log.error("No module registered for next stage %s", target_stage)
        match["stage"] = "finished"
        await save_match(match)
        runtime.clear(mid)
        return

    match = await get_match(mid)
    if not match or match.get("stage") != target_stage:
        return
    match["round_preparing"] = False
    await save_match(match)
    await refresh_lobby_scoreboard(context, mid, force=True)

    try:
        latest = await get_match(mid)
        gate_mode = ((latest or {}).get("next_round") or {}).get("mode")
        if target_stage == "final" and gate_mode == "final_bracket":
            await module._start_bracket_round(context, mid)
        else:
            await module.begin(context, mid)
    except Exception:
        log.exception("Failed to begin the next stage")


async def resume_pending_next_round_gates(application):
    from types import SimpleNamespace
    from .database import matches

    context = SimpleNamespace(bot=application.bot)
    try:
        cursor = matches.find({
            "stage": "intermission",
            "next_round.status": "open",
        }).batch_size(100)
        async for match in cursor:
            match_id = str(match.get("_id", ""))
            if not match_id:
                continue
            # Starting the same timeout task twice in one process is unnecessary.
            state = runtime.get(match_id)
            if any(not task.done() and task.get_name() == f"next-round-gate:{match_id}"
                   for task in state.get("tasks", [])):
                continue
            task = asyncio.create_task(
                _next_round_entry_timeout(context, match_id),
                name=f"next-round-gate:{match_id}",
            )
            runtime.add_task(match_id, task)
    except Exception:
        log.exception("Could not recover pending next-round entry windows")


async def open_final_bracket_gate(context, mid, qualified_ids):
    """Open a timed opt-in window before a later Final bracket round."""
    match = await get_match(mid)
    if not match or match.get("stage") != "final":
        return
    eligible_set = {str(uid) for uid in (qualified_ids or [])}
    eligible_ids = [
        player["id"] for player in match.get("players", [])
        if player.get("alive", True) and str(player.get("id")) in eligible_set
    ]
    if len(eligible_ids) <= 1:
        if eligible_ids:
            await crown(context, mid, eligible_ids[0])
        return

    now = time.time()
    entrants = {str(uid) for uid in (match.get("current_round_player_ids") or [])}
    if not entrants:
        entrants = {str(p.get("id")) for p in match.get("players", []) if p.get("alive", True)}
    qualified_ids = [p.get("id") for p in match.get("players", [])
                     if str(p.get("id")) in entrants and p.get("alive", True) and p.get("id") in eligible_set]
    eliminated_ids = [p.get("id") for p in match.get("players", [])
                      if str(p.get("id")) in entrants and str(p.get("id")) not in {str(uid) for uid in qualified_ids}]
    match.setdefault("round_results", {})["final"] = {
        "qualified_ids": qualified_ids,
        "eliminated_ids": eliminated_ids,
        "completed_at": now,
    }
    bracket_round = int((match.get("game") or {}).get("bracket_round", 1))
    final_round_id = f"final:{bracket_round}"
    _append_round_history(
        match, "final", final_round_id, qualified_ids, eliminated_ids, completed_at=now
    )
    # Persist the bracket result while this match is still in the Final stage.
    # If progression writes fail, startup can replay the pending history item.
    await save_match(match)
    await record_round_participation(
        mid, "final", qualified_ids, eliminated_ids, round_id=final_round_id,
    )
    _mark_round_stats_applied(match, final_round_id)
    await save_match(match)

    match["stage"] = "intermission"
    match["next_round"] = {
        "status": "open",
        "mode": "final_bracket",
        "from_stage": "final",
        "completed_title": f"Final · Bracket {int((match.get('game') or {}).get('bracket_round', 1))}",
        "eligible_ids": eligible_ids,
        "joined_ids": [],
        "opened_at": now,
        "deadline": now + max(1, int(config.ROUND_ENTRY_SECONDS)),
    }
    await save_match(match)
    await refresh_lobby_scoreboard(context, mid, force=True)
    match["next_round_message_id"] = None
    await _send_next_round_gate(context, match)
    runtime.add_task(mid, asyncio.create_task(_next_round_entry_timeout(context, mid), name=f"next-round-gate:{mid}"))

    async def notify(uid):
        try:
            await context.bot.send_message(
                uid,
                "✅ You qualified for the next Final bracket. Return to the group and "
                "tap ENTER NEXT ROUND before the timer expires.",
            )
        except Exception:
            pass

    await asyncio.gather(*(notify(uid) for uid in eligible_ids), return_exceptions=True)


async def advance(context, mid):
    """Start the initial round from the lobby; later rounds use the entry gate."""
    match = await get_match(mid)
    if not match or match.get("stage") == "finished":
        return

    players = alive(match)
    if len(players) == 0:
        match["stage"] = "finished"
        await record_match_result(
            mid, match.get("players", []), None, match.get("round_results"),
            chat_id=match.get("chat_id"), round_history=match.get("round_history", []),
            reason="no_survivors_at_start",
        )
        match["stats_finalized"] = True
        await save_match(match)
        runtime.clear(mid)
        await _edit_round_card(
            context, match, "🩸 <b>NO ONE SURVIVED THE GAMES.</b>", reply_markup=ui.kb([])
        )
        return
    if len(players) == 1:
        await crown(context, mid, players[0]["id"])
        return

    if match.get("stage") != "lobby":
        log.warning("advance ignored outside lobby; mid=%s stage=%s", mid, match.get("stage"))
        return

    target_stage = "rlgl"
    match["stage"] = target_stage
    match["current_round_stage"] = target_stage
    match["current_round_player_ids"] = [player.get("id") for player in players]
    match["round_preparing"] = True
    match.setdefault("round_results", {})
    await save_match(match)
    await record_game_start(match["chat_id"])
    await record_match_participation(mid, [player.get("id") for player in players if player.get("id") is not None])
    await refresh_lobby_scoreboard(context, mid, force=True)
    runtime.clear(mid)  
    module = _round_module(target_stage)
    if module:
        await module.begin(context, mid)


async def crown(context, mid, winner_id):
    """Idempotently finalize a champion even if several game callbacks race."""
    state = runtime.get(str(mid))
    async with state.setdefault("champion_lock", asyncio.Lock()):
        await _crown_once(context, mid, winner_id)


async def _crown_once(context, mid, winner_id):
    match = await get_match(mid)
    if not match:
        return
    if match.get("stage") == "finished" and match.get("stats_finalized"):
        return
    winner = get_player(match, winner_id)
    if not winner:
        log.error("Cannot crown missing winner %s in match %s", winner_id, mid)
        return
        
    current_stage = match.get("current_round_stage")
    round_results = match.setdefault("round_results", {})
    if current_stage:
        round_id = (
            f"final:{int((match.get('game') or {}).get('bracket_round', 1))}"
            if current_stage == "final" else current_stage
        )
        history_entry = next(
            (item for item in (match.get("round_history") or [])
             if str(item.get("round_id")) == str(round_id)),
            None,
        )
        if history_entry is None:
            entrant_ids = {str(uid) for uid in (match.get("current_round_player_ids") or [])}
            if not entrant_ids:
                entrant_ids = {str(p.get("id")) for p in match.get("players", []) if p.get("alive", True)}
            qualified = [winner_id] if str(winner_id) in entrant_ids else []
            eliminated = [
                p.get("id") for p in match.get("players", [])
                if str(p.get("id")) in entrant_ids and str(p.get("id")) != str(winner_id)
            ]
            round_results[current_stage] = {
                "qualified_ids": qualified,
                "eliminated_ids": eliminated,
                "completed_at": time.time(),
            }
            _append_round_history(
                match, current_stage, round_id, qualified, eliminated,
                completed_at=round_results[current_stage]["completed_at"],
            )
            history_entry = next(
                item for item in match["round_history"]
                if str(item.get("round_id")) == str(round_id)
            )
            # Durably save the outcome before the per-player updates.
            await save_match(match)

        # Only new records carry stats_applied=False. Missing is treated as legacy
        # history that predates the marker rather than replaying old events.
        if history_entry.get("stats_applied") is False:
            await record_round_participation(
                mid, current_stage,
                history_entry.get("qualified_ids") or [],
                history_entry.get("eliminated_ids") or [],
                round_id=round_id,
            )
            _mark_round_stats_applied(match, round_id)
            await save_match(match)


    match["stage"] = "finished"
    match["round_preparing"] = False
    match["champion_id"] = winner_id
    gate = match.get("next_round")
    if gate:
        gate["status"] = "closed"

    await record_match_result(
        mid, match.get("players", []), int(winner_id), match.get("round_results"),
        chat_id=match.get("chat_id"), round_history=match.get("round_history", []),
        reason="champion_crowned",
    )
    match["stats_finalized"] = True
    await save_match(match)
    await refresh_lobby_scoreboard(context, mid, force=True)
    
    # 🟢 Match board unpin with a 4-second delay after winning
    scoreboard_id = match.get("scoreboard_message_id")
    if scoreboard_id:
        _spawn_background_task(
            delayed_unpin_board(context, match["chat_id"], scoreboard_id, delay=4.0),
            name=f"unpin-board:{mid}",
        )

    runtime.clear(mid)

    winner_name = html.escape(str(winner.get("name") or winner_id))
    winner_emoji = html.escape(str(winner.get("emoji") or "🏆"))
    text = (
        "🏆 <b>SQUID GAME CHAMPION</b> 🏆\n\n"
        f"{winner_emoji} <b>{winner_name}</b> is the last one standing!\n\n"
        "🩸 The games are over. Use /enterthehell to play again."
    )
    
    try:
        message = await group_send(context, match, text, reply_markup=ui.kb([]))
        if message:
            match["round_message_id"] = message.message_id
            await save_match(match)
    except Exception as e:
        log.exception("Failed to send champion message: %s", e)



async def cleanup_interrupted_games(application):
    """Persist and terminate active games that lost process-local runtime state."""
    from types import SimpleNamespace
    from .database import matches

    context = SimpleNamespace(bot=application.bot)
    processed = 0
    try:
        cursor = matches.find({
            "stage": {"$nin": ["lobby", "intermission", "finished"]}
        }).batch_size(100)
        async for match in cursor:
            mid = str(match.get("_id", ""))
            if not mid:
                continue
            processed += 1
            try:
                await record_match_interruption(
                    mid, match.get("players", []), match.get("round_results"),
                    chat_id=match.get("chat_id"),
                    round_history=match.get("round_history", []),
                    reason="interrupted_by_restart",
                )
            except Exception:
                # Still attempt to close a stale match; a history failure is logged.
                log.exception("Could not persist interruption history for match %s", mid)

            match["stage"] = "finished"
            match["stats_finalized"] = True
            match.setdefault("game", {})["round_finished"] = True
            closed_text = (
                "🛑 <b>GAME TERMINATED</b>\n\n"
                "The bot server restarted while this round was in progress.\n"
                "Please start a new lobby with /enterthehell."
            )
            try:
                await save_match(match)
                runtime.clear(mid)
                await refresh_lobby_scoreboard(context, mid, force=True)
                await _edit_round_card(context, match, closed_text, reply_markup=ui.kb([]))
            except Exception:
                log.exception("Failed to clean up interrupted match %s", mid)
    except Exception:
        log.exception("Could not scan interrupted games during startup")
        return
    if processed:
        log.info("Cleaned up %s interrupted match(es).", processed)
