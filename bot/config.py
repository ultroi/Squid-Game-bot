"""Central configuration with defensive parsing for environment values."""
from __future__ import annotations

import os
from dotenv import load_dotenv

load_dotenv()


def _int_env(
    name: str,
    default: int,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    """Read an integer environment value without crashing on malformed input."""
    try:
        value = int(os.getenv(name, str(default)).strip())
    except (TypeError, ValueError):
        value = default
    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()  # from @BotFather
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017").strip() or "mongodb://localhost:27017"
DB_NAME = os.getenv("DB_NAME", "squid_game_bot").strip() or "squid_game_bot"
DB_MAX_POOL_SIZE = _int_env("DB_MAX_POOL_SIZE", 50, minimum=5)
DB_MIN_POOL_SIZE = _int_env("DB_MIN_POOL_SIZE", 0, minimum=0, maximum=DB_MAX_POOL_SIZE)
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO"
BOT_ADMIN_ID = _int_env("BOT_ADMIN_ID", 6439532660, minimum=0)

WELCOME_VIDEO = os.getenv("WELCOME_VIDEO", "assets/welcome.mp4").strip() or "assets/welcome.mp4"

# ---- lobby ----
MAX_PLAYERS = 20
MIN_PLAYERS = _int_env("MIN_PLAYERS", 2, minimum=2, maximum=MAX_PLAYERS)
LOBBY_SECONDS = _int_env("LOBBY_SECONDS", 60, minimum=10)
ROUND_ENTRY_SECONDS = _int_env("ROUND_ENTRY_SECONDS", 30, minimum=5)
ROUND_PREPARATION_SECONDS = _int_env("ROUND_PREPARATION_SECONDS", 5, minimum=1)

# ---- game 1: red light green light ----
RLGL_SECONDS = 60
GRID_SIZE = 10

# ---- game 2: dalgona ----
DALGONA_SECONDS = 120

# ---- game 3: marbles ----
MARBLES_SECONDS = 180
MARBLES_START = 10

# ---- game 4: glass bridge (hopscotch) ----
BRIDGE_SECONDS = 300
BRIDGE_LENGTH = 20

# ---- game 5: elimination vote ----
VOTE_SECONDS = 60

# ---- game 6: squid game final ----
FINAL_HP = 100
FINAL_DAMAGE = 10

PLAYER_EMOJIS = [
    "🦊", "🐼", "🐯", "🦁", "🐸", "🐙", "🦄", "🐝", "🦖", "🐳",
    "🦉", "🐺", "🐨", "🐰", "🐷", "🐵", "🦋", "🐢", "🦜", "🐬",
]

WELCOME_CAPTION = (
    "🔺 <b>WELCOME TO SQUID GAME BOT</b> 🔺\n\n"
    "🎮 Welcome, Players! This is a fun version of Squid Game where you can "
    "challenge your friends and compete in thrilling survival games.\n\n"
    "🩸 Play smart, survive every round, and fight to be the last one standing!\n\n"
    "Good luck, Players. Let the games begin! 🔴🔺🟥\n"
    "💡 Use /help to see available commands and how to play."
)