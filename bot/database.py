
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

async def ensure_indexes() -> None:
    await users.create_index(
        [("telegram_id", ASCENDING)],
        unique=True,
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

    # MongoDB's built-in unique _id index identifies the welcome asset.
    log.info("MongoDB indexes ensured")


def close_database() -> None:
    """Close the shared MongoDB connection pool during graceful shutdown."""
    client.close()
