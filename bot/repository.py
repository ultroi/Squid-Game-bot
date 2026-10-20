"""All MongoDB access in one place + small match-document helpers."""
import html
from datetime import datetime, timezone
import asyncio
from bson import ObjectId
from bson.errors import InvalidId
from pymongo import ReturnDocument

from .database import users, matches, welcome_assets, db, bot_stats, groups


def now() -> datetime:
    return datetime.now(timezone.utc)

WELCOME_VIDEO_KEY = "welcome_video"

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
        "stage": {"$nin": ["lobby", "finished"]}, 
        "players": {
            "$elemMatch": {
                "id": user_id,
                "alive": True
            }
        }
    }, projection={"_id": 1})
    
    return active_match is not None

async def add_win(telegram_id: int) -> None:
    await users.update_one({"telegram_id": telegram_id}, {"$inc": {"wins": 1}})


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

    # 2. Delayed Cleanup: If finished, wipe it from MongoDB after 60 seconds
    if match.get("stage") == "finished":
        async def delayed_delete():
            await asyncio.sleep(60)  # Give the UI plenty of time to send final notices
            await matches.delete_one({"_id": match_id})
            
        asyncio.create_task(delayed_delete())

        

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
        "active_games": await matches.count_documents({"stage": {"$ne": "lobby"}}),
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