"""All MongoDB access in one place + small match-document helpers."""
import html
from datetime import datetime, timedelta, timezone
import asyncio
import logging
import time
from bson import ObjectId
from bson.errors import InvalidId
from pymongo import ReturnDocument

from .database import users, matches, welcome_assets, db, bot_stats, groups, match_history


def now() -> datetime:
    return datetime.now(timezone.utc)

log = logging.getLogger(__name__)

WELCOME_VIDEO_KEY = "welcome_video"

# Keep deduplication state bounded so one player document cannot grow forever.
# Duplicate retries are rejected for every event retained in this rolling window.
_MAX_PROGRESSION_EVENTS = 2000
_MATCH_DELETE_TASKS: dict[str, asyncio.Task] = {}


async def _apply_progression_event(telegram_id: int, event_id: str, increments: dict) -> None:
    """Atomically apply one per-user event, if its dedupe key is still retained."""
    await users.update_one(
        {"telegram_id": int(telegram_id), "progression_events": {"$ne": event_id}},
        {
            "$inc": increments,
            "$push": {
                "progression_events": {
                    "$each": [event_id],
                    "$slice": -_MAX_PROGRESSION_EVENTS,
                }
            },
        },
    )

# ---------- users ----------

async def upsert_user(tg_user) -> dict:
    return await users.find_one_and_update(
        {"telegram_id": tg_user.id},
        {"$set": {"username": tg_user.username,
                  "first_name": tg_user.first_name,
                  "last_active": now()},
         "$setOnInsert": {"wins": 0, "created_at": now()}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )


async def is_user_in_other_active_match(user_id: int, current_match_id: str) -> bool:
    """Check if a user is ALIVE in any other game that is actively playing."""
    try:
        current_obj_id = ObjectId(current_match_id)
    except (InvalidId, TypeError):
        return False
        
    active_match = await matches.find_one({
        "_id": {"$ne": current_obj_id},
        # Include other lobbies too; otherwise the same player can register
        # in multiple groups and end up entering concurrent matches.
        "stage": {"$ne": "finished"}, 
        "players": {
            "$elemMatch": {
                "id": user_id,
                "alive": True
            }
        }
    }, projection={"_id": 1})
    
    return active_match is not None

async def add_win(telegram_id: int) -> None:
    """Legacy helper; prefer record_match_result so wins are idempotent.

    Do not call this in addition to record_match_result for the same match, or
    a champion's wins could be incremented twice.
    """
    await users.update_one({"telegram_id": telegram_id}, {"$inc": {"wins": 1}})


async def get_player_profile(telegram_id: int) -> dict | None:
    """Return persistent overall and per-round progression for /mystats."""
    return await users.find_one(
        {"telegram_id": telegram_id},
        {
            "_id": 0,
            "telegram_id": 1,
            "username": 1,
            "first_name": 1,
            "wins": 1,
            "matches_played": 1,
            "matches_completed": 1,
            "matches_won": 1,
            "matches_lost": 1,
            "rounds_played": 1,
            "rounds_survived": 1,
            "rounds_eliminated": 1,
            "round_stats": 1,
            "created_at": 1,
            "selected_tag": 1,
            "xp": 1,
        },
    )


ROUND_STAGE_KEYS = ("rlgl", "dalgona", "marbles", "bridge", "vote", "final")
ROUND_STAGE_LABELS = {
    "rlgl": "Red Light, Green Light",
    "dalgona": "Dalgona",
    "marbles": "Marbles",
    "bridge": "Glass Bridge",
    "vote": "Voting",
    "final": "Final",
}


async def record_match_participation(match_id: str, player_ids: list[int]) -> None:
    """Count initial match entrants once; run independent user writes concurrently."""
    await asyncio.gather(*(
        _apply_progression_event(
            telegram_id, f"match:{match_id}:joined:{telegram_id}",
            {"matches_played": 1},
        )
        for telegram_id in set(player_ids)
    ))


async def record_round_participation(
    match_id: str,
    stage: str,
    qualified_ids: list[int],
    eliminated_ids: list[int],
    *,
    round_id: str | None = None,
) -> None:
    """Persist round wins/losses and totals once per player and round instance.

    A qualified player gets a round win; an eliminated player gets a round loss.
    ``round_id`` distinguishes repeated Final brackets in the same match.
    """
    stage_key = stage.split(":", 1)[0].lower()
    if stage_key not in ROUND_STAGE_KEYS:
        # Unknown stages should not silently pollute the per-round leaderboards.
        return
    event_round = round_id or stage
    # Normalize to disjoint sets. If malformed input contains the same ID in both,
    # qualification wins; one player must never count both a win and a loss for one round.
    qualified = {int(uid) for uid in (qualified_ids or []) if uid is not None}
    eliminated = {int(uid) for uid in (eliminated_ids or []) if uid is not None} - qualified
    outcomes = [(uid, True) for uid in qualified]
    outcomes.extend((uid, False) for uid in eliminated)
    async def apply_outcome(telegram_id: int, survived: bool) -> None:
        event_id = f"round:{match_id}:{event_round}:{telegram_id}"
        increments = {
            "rounds_played": 1,
            "rounds_survived" if survived else "rounds_eliminated": 1,
            f"round_stats.{stage_key}.played": 1,
            # Incrementing by zero initializes both fields for deterministic sorting.
            f"round_stats.{stage_key}.wins": 1 if survived else 0,
            f"round_stats.{stage_key}.losses": 0 if survived else 1,
        }
        await _apply_progression_event(telegram_id, event_id, increments)

    # A match supports at most 20 players. Parallel independent writes remove a
    # per-round latency of 20 sequential MongoDB round trips without sharing docs.
    await asyncio.gather(*(apply_outcome(uid, survived) for uid, survived in outcomes))


async def _apply_match_result_counters(
    match_id: str, player_ids: list[int], winner_id: int | None
) -> None:
    """Apply one completed match outcome per user with retry-safe event keys."""
    async def apply_result(telegram_id: int) -> None:
        is_winner = winner_id is not None and telegram_id == int(winner_id)
        event_id = f"match-result:{match_id}:{telegram_id}"
        increments = {"matches_completed": 1}
        if is_winner:
            increments.update({"wins": 1, "matches_won": 1})
        else:
            increments["matches_lost"] = 1
        await _apply_progression_event(telegram_id, event_id, increments)

    await asyncio.gather(*(apply_result(uid) for uid in set(player_ids)))


async def record_match_result(
    match_id: str,
    players: list[dict],
    winner_id: int | None,
    round_results: dict | None,
    *,
    chat_id: int | None = None,
    round_history: list[dict] | None = None,
    finished_at: float | None = None,
    reason: str = "completed",
) -> None:
    """Persist match outcome first, then update counters and mark them applied.

    A false ``stats_applied`` marker is a durable retry record: startup recovery can
    resume a partial cross-document update without inventing a second match result.
    """
    player_snapshot: list[dict] = []
    player_ids: list[int] = []
    for player in players or []:
        uid = player.get("id")
        if uid is None:
            continue
        uid = int(uid)
        if uid in player_ids:
            continue
        player_ids.append(uid)
        player_snapshot.append({
            "telegram_id": uid,
            "name": str(player.get("name") or f"Player {uid}"),
            "emoji": str(player.get("emoji") or ""),
        })

    finished_epoch = float(finished_at or time.time())
    history_id = str(match_id)
    history_doc = {
        "match_id": history_id,
        "chat_id": chat_id,
        "champion_id": int(winner_id) if winner_id is not None else None,
        "players": player_snapshot,
        "round_results": round_results or {},
        "round_history": round_history or [],
        "result": "champion" if winner_id is not None else "no_champion",
        "reason": reason,
        "finished_at": finished_epoch,
        "stats_applied": False,
    }
    await match_history.update_one(
        {"_id": history_id}, {"$setOnInsert": history_doc}, upsert=True
    )

    # The first persisted result is canonical. In particular, do not let a late
    # completion callback convert a cancellation/restart record into a loss.
    stored = await match_history.find_one(
        {"_id": history_id},
        {"result": 1, "champion_id": 1, "players": 1, "stats_applied": 1, "match_id": 1},
    )
    if not stored or stored.get("result") == "interrupted" or stored.get("stats_applied") is True:
        return

    canonical_ids = list(dict.fromkeys(
        int(player["telegram_id"])
        for player in (stored.get("players") or player_snapshot)
        if player.get("telegram_id") is not None
    ))
    canonical_winner = stored.get("champion_id")
    await _apply_match_result_counters(
        str(stored.get("match_id") or history_id), canonical_ids, canonical_winner
    )
    await match_history.update_one(
        {"_id": history_id, "stats_applied": {"$ne": True}},
        {"$set": {"stats_applied": True, "stats_applied_at": now()}},
    )


async def reconcile_pending_match_statistics(limit: int = 1000) -> int:
    """Retry completed match counters whose durable history is still pending."""
    limit = max(1, min(int(limit), 10000))
    cursor = match_history.find({
        "stats_applied": False,
        "result": {"$in": ["champion", "no_champion"]},
    }).batch_size(100)
    reconciled = 0
    async for record in cursor:
        match_id = str(record.get("match_id") or record.get("_id") or "")
        if not match_id:
            continue
        player_ids = list(dict.fromkeys(
            int(player["telegram_id"])
            for player in (record.get("players") or [])
            if player.get("telegram_id") is not None
        ))
        try:
            await _apply_match_result_counters(
                match_id, player_ids, record.get("champion_id")
            )
            result = await match_history.update_one(
                {"_id": record["_id"], "stats_applied": False},
                {"$set": {"stats_applied": True, "stats_applied_at": now()}},
            )
            reconciled += int(result.modified_count > 0)
        except Exception:
            log.exception("Could not reconcile match stats for %s", match_id)
        if reconciled >= limit:
            break
    return reconciled


async def record_match_interruption(
    match_id: str,
    players: list[dict],
    round_results: dict | None,
    *,
    chat_id: int | None = None,
    round_history: list[dict] | None = None,
    reason: str = "interrupted",
) -> None:
    """Persist an aborted/interrupted match without misclassifying it as a loss.

    This record is deliberately separate from player completion counters: a
    cancellation, restart or failed setup is not a completed match outcome.
    """
    snapshots = []
    seen = set()
    for player in players or []:
        uid = player.get("id")
        if uid is None:
            continue
        uid = int(uid)
        if uid in seen:
            continue
        seen.add(uid)
        snapshots.append({
            "telegram_id": uid,
            "name": str(player.get("name") or f"Player {uid}"),
            "emoji": str(player.get("emoji") or ""),
        })

    document = {
        "match_id": str(match_id),
        "chat_id": chat_id,
        "champion_id": None,
        "players": snapshots,
        "round_results": round_results or {},
        "round_history": round_history or [],
        "result": "interrupted",
        "reason": reason,
        "finished_at": time.time(),
    }
    # $setOnInsert preserves the first terminal outcome and tolerates retries.
    try:
        await match_history.update_one(
            {"_id": str(match_id)}, {"$setOnInsert": document}, upsert=True
        )
    except Exception as exc:
        # Concurrent terminal handlers can race to insert the same match.
        # Only retry duplicate-key races; propagate genuine database failures.
        from pymongo.errors import DuplicateKeyError
        if not isinstance(exc, DuplicateKeyError):
            raise
        await match_history.update_one(
            {"_id": str(match_id)}, {"$setOnInsert": document}, upsert=False
        )


async def get_leaderboard(limit: int = 10, scope: str = "overall") -> list[dict]:
    """Return real MongoDB rankings for overall wins or one round category."""
    allowed_scopes = set(ROUND_STAGE_KEYS) | {"overall"}
    if scope not in allowed_scopes:
        scope = "overall"
    limit = max(1, min(int(limit), 25))
    projection = {
        "_id": 0,
        "telegram_id": 1,
        "username": 1,
        "first_name": 1,
        "wins": 1,
        "matches_played": 1,
        "matches_won": 1,
        "matches_lost": 1,
        "rounds_played": 1,
        "rounds_survived": 1,
        "rounds_eliminated": 1,
        "round_stats": 1,
    }
    if scope == "overall":
        query = {"wins": {"$gt": 0}}
        sort = [("wins", -1), ("rounds_survived", -1), ("telegram_id", 1)]
    else:
        field = f"round_stats.{scope}"
        query = {f"{field}.played": {"$gt": 0}}
        sort = [(f"{field}.wins", -1), (f"{field}.losses", 1), (f"{field}.played", -1), ("telegram_id", 1)]
    cursor = users.find(query, projection).sort(sort).limit(limit)
    return await cursor.to_list(length=limit)


async def get_player_rank(telegram_id: int, wins: int | None = None) -> int | None:
    """Return overall leaderboard rank, or None for players with no wins.

    The overall leaderboard only includes players with at least one full-match win,
    so the profile must use the same eligibility rule rather than assigning a rank
    below the champions to a zero-win player.
    """
    profile = await get_player_profile(telegram_id)
    if not profile:
        return None

    # Prefer the current database value to avoid calculating a rank from stale UI data.
    rank_wins = int(profile.get("wins", wins if wins is not None else 0) or 0)
    if rank_wins <= 0:
        return None

    survived = int(profile.get("rounds_survived", 0) or 0)
    ahead = await users.count_documents({
        "wins": {"$gt": 0},
        "$or": [
            {"wins": {"$gt": rank_wins}},
            {"wins": rank_wins, "rounds_survived": {"$gt": survived}},
            {"wins": rank_wins, "rounds_survived": survived, "telegram_id": {"$lt": telegram_id}},
        ],
    })
    return ahead + 1


async def get_player_rank_for_scope(telegram_id: int, scope: str = "overall") -> int | None:
    """Return the caller's one-based rank in a selected leaderboard category."""
    profile = await get_player_profile(telegram_id)
    if not profile:
        return None
    if scope == "overall":
        wins = int(profile.get("wins", 0) or 0)
        survived = int(profile.get("rounds_survived", 0) or 0)
        if wins <= 0:
            return None
        ahead_query = {
            "wins": {"$gt": 0},
            "$or": [
                {"wins": {"$gt": wins}},
                {"wins": wins, "rounds_survived": {"$gt": survived}},
                {"wins": wins, "rounds_survived": survived, "telegram_id": {"$lt": telegram_id}},
            ],
        }
    elif scope in ROUND_STAGE_KEYS:
        stats = (profile.get("round_stats") or {}).get(scope) or {}
        if int(stats.get("played", 0) or 0) <= 0:
            return None
        wins = int(stats.get("wins", 0) or 0)
        losses = int(stats.get("losses", 0) or 0)
        field = f"round_stats.{scope}"
        ahead_query = {
            f"{field}.played": {"$gt": 0},
            "$or": [
                {f"{field}.wins": {"$gt": wins}},
                {f"{field}.wins": wins, f"{field}.losses": {"$lt": losses}},
                {f"{field}.wins": wins, f"{field}.losses": losses, f"{field}.played": {"$gt": int(stats.get("played", 0) or 0)}},
                {f"{field}.wins": wins, f"{field}.losses": losses, f"{field}.played": int(stats.get("played", 0) or 0), "telegram_id": {"$lt": telegram_id}},
            ],
        }
    else:
        return None
    return int(await users.count_documents(ahead_query)) + 1


# ---------- matches ----------

async def create_match(chat_id: int, host_id: int) -> dict:
    doc = {
        "chat_id": chat_id,
        "host_id": host_id,
        "stage": "lobby",          
        "players": [],
        "lobby_message_id": None,
        "board_message_id": None,
        # Track round announcements, their result messages, and the separate
        # next-round entry gate so each can be edited/deleted independently.
        "round_message_id": None,
        "round_message_stage": None,
        "round_result_message_id": None,
        "round_result_message_stage": None,
        "next_round_message_id": None,
        "game": {},
        "created_at": now(),
        "updated_at": now(),
    }
    res = await matches.insert_one(doc)
    doc["_id"] = res.inserted_id
    return doc


async def get_match(match_id: str):
    try:
        object_id = ObjectId(match_id)
    except (InvalidId, TypeError):
        return None
    return await matches.find_one({"_id": object_id})

async def save_match(match: dict) -> None:
    """Persist an existing match and schedule cleanup if it is finished."""
    match_id = match.get("_id")
    if match_id is None:
        raise ValueError("Cannot save a match without _id")

    # 1. Update the document normally so final UI refreshes can read it
    match["updated_at"] = now()
    fields = {key: value for key, value in match.items() if key != "_id"}
    result = await matches.update_one({"_id": match_id}, {"$set": fields}, upsert=False)
    
    if result.matched_count == 0:
        raise LookupError(f"Match {match_id} no longer exists in MongoDB")

    # 2. Delayed cleanup: keep the last board/update visible briefly, then delete.
    # Hold a strong task reference and reset the timer on later terminal saves.
    if match.get("stage") == "finished":
        key = str(match_id)
        existing_task = _MATCH_DELETE_TASKS.get(key)
        if existing_task is not None and not existing_task.done():
            existing_task.cancel()

        async def delayed_delete() -> None:
            try:
                await asyncio.sleep(60)
                await matches.delete_one({"_id": match_id})
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Could not delete finished match %s", match_id)
            finally:
                current = asyncio.current_task()
                if _MATCH_DELETE_TASKS.get(key) is current:
                    _MATCH_DELETE_TASKS.pop(key, None)

        task = asyncio.create_task(delayed_delete(), name=f"delete-finished-match:{key}")
        _MATCH_DELETE_TASKS[key] = task


async def cleanup_stale_finished_matches(grace_seconds: int = 60) -> int:
    """Remove terminal match docs whose delayed-delete task was lost on restart."""
    cutoff = now() - timedelta(seconds=max(0, int(grace_seconds)))
    result = await matches.delete_many({
        "stage": "finished",
        "updated_at": {"$lt": cutoff},
    })
    return int(result.deleted_count)

        

async def save_rlgl_players(match_id: str, players: list, last_event: str = None) -> None:
    """One focused write: only `players` + `game.last_event` + `updated_at`."""
    try:
        object_id = ObjectId(match_id)
    except (InvalidId, TypeError):
        return
    set_fields = {"updated_at": now(), "players": players}
    if last_event is not None:
        set_fields["game.last_event"] = last_event
    await matches.update_one({"_id": object_id}, {"$set": set_fields})


async def update_dalgona_tap(match_id: str, user_id: int, player_updates: dict, dalgona_updates: dict, last_event: str = None) -> None:
    """Granular update for fast Dalgona taps. Bypasses full match serialization."""
    try:
        object_id = ObjectId(match_id)
    except (InvalidId, TypeError):
        return
    
    set_fields = {"updated_at": now()}
    
    # 1. Array updates (e.g. setting player["alive"] = False)
    for k, v in player_updates.items():
        set_fields[f"players.$[elem].{k}"] = v
        
    # 2. Dictionary updates inside game.players.<uid>
    uid_str = str(user_id)
    for k, v in dalgona_updates.items():
        set_fields[f"game.players.{uid_str}.{k}"] = v
        
    if last_event:
        set_fields["game.last_event"] = last_event
        
    # Only use array_filters if we are mutating the main players array
    filters = [{"elem.id": user_id}] if player_updates else None
    
    await matches.update_one(
        {"_id": object_id},
        {"$set": set_fields},
        array_filters=filters
    )


async def get_active_match(chat_id: int):
    return await matches.find_one(
        {"chat_id": chat_id, "stage": {"$ne": "finished"}},
        projection={"_id": 1},
        sort=[("created_at", -1)],
    )


async def get_bot_stats() -> dict:
    """Fetch efficient stats using live active counts and persistent totals."""
    stats = await bot_stats.find_one({"_id": "global"}) or {}
    
    return {
        "total_games": stats.get("total_games", 0),
        # Count in-progress matches, excluding both waiting lobbies and terminal documents
        # retained briefly for UI cleanup. Intermission/next-round gates remain active.
        "active_games": await matches.count_documents({"stage": {"$nin": ["lobby", "finished"]}}),
        "total_users": await users.estimated_document_count(),
        "total_groups": await groups.count_documents({"active": True})
    }


async def record_game_start(chat_id: int) -> None:
    """Increment global stats only when a game successfully starts."""
    await bot_stats.update_one({"_id": "global"}, {"$inc": {"total_games": 1}}, upsert=True)
    await groups.update_one({"_id": chat_id}, {"$set": {"active": True}}, upsert=True)

# ---------- match helpers ----------

def alive(match: dict) -> list[dict]:
    return [p for p in match.get("players", []) if p.get("alive", True)]


def get_player(match: dict, uid: int):
    return next((p for p in match["players"] if p["id"] == uid), None)


def pname(match: dict, uid: int) -> str:
    p = get_player(match, uid)
    return f"{p['emoji']} {html.escape(p['name'])}" if p else "?"

async def get_welcome_video_file_id() -> str | None:
    document = await welcome_assets.find_one({"_id": WELCOME_VIDEO_KEY}, {"file_id": 1})
    return document.get("file_id") if document else None

async def save_welcome_video_file_id(file_id: str) -> None:
    await welcome_assets.update_one(
        {"_id": WELCOME_VIDEO_KEY},
        {"$set": {"file_id": file_id, "updated_at": now()}},
        upsert=True,
    )

async def clear_welcome_video_file_id() -> None:
    await welcome_assets.delete_one({"_id": WELCOME_VIDEO_KEY})