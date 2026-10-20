import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()  # from @BotFather
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
DB_NAME = os.getenv("DB_NAME", "squid_game_bot")
DB_MAX_POOL_SIZE = max(5, int(os.getenv("DB_MAX_POOL_SIZE", "50")))
DB_MIN_POOL_SIZE = max(0, min(DB_MAX_POOL_SIZE, int(os.getenv("DB_MIN_POOL_SIZE", "0"))))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")


WELCOME_VIDEO = os.getenv("WELCOME_VIDEO", "assets/welcome.mp4")

# ---- lobby ----
MIN_PLAYERS = max(2, int(os.getenv("MIN_PLAYERS", "2")))
MAX_PLAYERS = 20
LOBBY_SECONDS = max(10, int(os.getenv("LOBBY_SECONDS", "60")))
ROUND_ENTRY_SECONDS = max(5, int(os.getenv("ROUND_ENTRY_SECONDS", "30")))
ROUND_PREPARATION_SECONDS = max(1, int(os.getenv("ROUND_PREPARATION_SECONDS", "5")))

# ---- game 1: red light green light ----
RLGL_SECONDS = 90
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

PLAYER_EMOJIS = ["🦊", "🐼", "🐯", "🦁", "🐸", "🐙", "🦄", "🐝", "🦖", "🐳",
                 "🦉", "🐺", "🐨", "🐰", "🐷", "🐵", "🦋", "🐢", "🦜", "🐬"]

WELCOME_CAPTION = (
    "🔺 <b>WELCOME TO SQUID GAME BOT</b> 🔺\n\n"
    "🎮 Welcome, Players! This is a fun version of Squid Game where you can "
    "challenge your friends and compete in thrilling survival games.\n\n"
    "🩸 Play smart, survive every round, and fight to be the last one standing!\n\n"
    "Good luck, Players. Let the games begin! 🔴🔺🟥"
)
