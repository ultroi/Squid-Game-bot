
import logging

from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ASCENDING

from .config import MONGO_URI, DB_NAME, DB_MAX_POOL_SIZE, DB_MIN_POOL_SIZE

log = logging.getLogger(__name__)

# One shared async MongoDB client for the whole process.
client = AsyncIOMotorClient(
    MONGO_URI,
    maxPoolSize=DB_MAX_POOL_SIZE,
    minPoolSize=DB_MIN_POOL_SIZE,
    serverSelectionTimeoutMS=5000,
    connectTimeoutMS=10000,
    retryWrites=True,
)

db = client[DB_NAME]

users = db["users"]
matches = db["matches"]
welcome_assets = db["welcome_assets"]
bot_stats = db["bot_stats"]
groups = db["groups"]
match_history = db["match_history"]
broadcast_jobs = db["broadcast_jobs"]

async def ensure_indexes() -> None:
    await users.create_index(
        [("telegram_id", ASCENDING)],
        unique=True,
    )
    await users.create_index(
        [("wins", -1), ("telegram_id", ASCENDING)],
        name="player_wins_rank",
    )
    # Supports group match lookup and picks the newest match deterministically.
    await matches.create_index(
        [("chat_id", ASCENDING), ("stage", ASCENDING), ("created_at", -1)],
        name="chat_stage_created",
    )
    # Used during startup recovery of pending entry gates.
    await matches.create_index(
        [("stage", ASCENDING), ("next_round.status", ASCENDING)],
        name="stage_gate_status",
    )

    # Round-specific leaderboard indexes keep category leaderboards responsive.
    for stage in ("rlgl", "dalgona", "marbles", "bridge", "vote", "final"):
        await users.create_index(
            [(f"round_stats.{stage}.wins", -1), (f"round_stats.{stage}.losses", 1), ("telegram_id", 1)],
            name=f"round_rank_{stage}",
        )
    await match_history.create_index([("chat_id", 1), ("finished_at", -1)], name="match_history_chat_finished")
    await match_history.create_index(
        [("stats_applied", ASCENDING), ("finished_at", ASCENDING)],
        name="match_history_pending_stats",
    )
    await match_history.create_index(
        [("round_history.stats_applied", ASCENDING)],
        name="match_history_pending_round_stats",
    )
    await matches.create_index(
        [("stage", ASCENDING), ("round_history.stats_applied", ASCENDING)],
        name="match_pending_round_stats",
    )

    # MongoDB's built-in unique _id index identifies the welcome asset.
    log.info("MongoDB indexes ensured")


def close_database() -> None:
    """Close the shared MongoDB connection pool during graceful shutdown."""
    client.close()
