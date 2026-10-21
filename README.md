# 🦑 Squid Game Bot for Telegram

A multiplayer, elimination-style survival game for Telegram groups. Players enter a lobby, compete through a sequence of mini-games, qualify for the next round, and try to become the last player standing. The group chat carries the lobby, shared game announcements, match board, and round-entry buttons; most player actions happen in the bot's private chat using inline controls.

Built with **Python 3.11+**, **python-telegram-bot 21.6 (async)**, and **MongoDB** through Motor.

## Table of contents

- [Game at a glance](#game-at-a-glance)
- [How a match progresses](#how-a-match-progresses)
- [The rounds](#the-rounds)
- [Getting started](#getting-started)
- [Commands](#commands)
- [Configuration](#configuration)
- [How the code is organized](#how-the-code-is-organized)
- [Persistence, hosting, and recovery](#persistence-hosting-and-recovery)
- [Troubleshooting](#troubleshooting)

## Game at a glance

- **Players:** 2–20 by default; the minimum can be raised with `MIN_PLAYERS`. The maximum is currently fixed at 20 in `bot/config.py`.
- **Game mode:** One active match per group. A player cannot join another active match while still alive in one.
- **Group controls:** Create a lobby, join, start when eligible, review the shared round card and results, and enter the next round.
- **Private controls:** Movement, Dalgona sequence input, Marbles choices, Bridge choices, voting, and Final combat are sent to each player's DM.
- **Elimination:** Players who fail a round, miss the next-round entry window, or do not finish an applicable timed objective are removed from the match.
- **Winner:** The final remaining champion is announced in the group. The winner's win count is saved in MongoDB.

> **Important:** Every player must open the bot in a private chat and send `/start` before joining. Telegram bots cannot start a private conversation with a user who has never started the bot. The host must also tap **Join** if they want to play; creating the lobby does not add the host automatically.

## How a match progresses

```text
/enterthehell
      │
      ▼
   Lobby ── minimum players reached ──► Red Light, Green Light
                                            │
                                            ▼
                                      Dalgona Candy
                                            │
                                            ▼
                                         Marbles
                                            │
                                            ▼
                                       Glass Bridge
                                            │
                              qualified players enter next round
                                            │
                       ┌────────────────────┴───────────────────┐
                       │                                        │
                Odd number enters                       Even number enters
                       │                                        │
                       ▼                                        │
                Elimination Vote                                │
                       │                                        │
                       └────────────────────┬───────────────────┘
                                            ▼
                                    Squid Game Final
                                            │
                                  repeat Final brackets
                                            │
                                            ▼
                                         Champion
```

The **Vote** round is conditional. After Glass Bridge, the bot counts the players who actually press **ENTER NEXT ROUND** before the entry window closes. If two or more players enter and that number is odd, a vote is inserted before the Final. If the number is even, the match proceeds directly to the Final.

### Lobby and next-round entry rules

1. A user runs `/enterthehell` in a group. The bot posts a lobby with a countdown and **Join** button.
2. Each participant taps **Join**. The bot attempts to send them a private confirmation; if the bot cannot DM them, they must open the bot and send `/start` before trying again.
3. Once the configured minimum is reached, the host can tap **Start Now**. If the host does not start manually, the lobby starts automatically when the countdown ends, provided enough players joined. Otherwise, the lobby closes.
4. After a round, surviving players are eligible for the next round. The bot posts a separate **ENTER NEXT ROUND** button with a countdown (30 seconds by default).
5. Eligible players must enter explicitly. Anyone who does not enter is removed from the match. If no one enters, the game ends; if exactly one player enters, that player is crowned champion without another mini-game. Two or more entrants start the next stage.
6. The group match board tracks player counts and round results. One active match can run in a group at a time.

## The rounds

### Round 1 — 🔴🟢 Red Light, Green Light

**Objective:** Reach the finish line before the 90-second race timer expires.

- Each player receives a private race board with a **10 × 10 grid**, player positions, progress, standings, the current light, and movement controls.
- Use **MOVE FORWARD**, **LEFT**, or **RIGHT** to navigate the grid. The light changes at random intervals.
- Move while the light is green. If your move is processed under a red-light phase, you are eliminated.
- Players must reach the top/finish row while alive. Anyone who has not finished when time expires is eliminated.
- Movement controls have a 0.5-second per-player cooldown to make rapid repeated taps less effective.

The group contains the round announcement and key race updates; individual movement and the detailed live grid are handled in each player's private board.

### Round 2 — 🍪 Dalgona Candy

**Objective:** Memorize the flashing tile sequence and repeat it correctly in your DM.

At the start of Dalgona, the bot randomly selects **one** shape challenge for that match. It does not run all four shapes in sequence.

| Random shape | Emoji | Sequence length | Approximate input time |
|---|---:|---:|---:|
| Circle | ⭕ | 4 taps | 11 seconds |
| Triangle | 🔺 | 6 taps | 14 seconds |
| Star | ⭐ | 8 taps | 17 seconds |
| Umbrella | ☂️ | 10 taps | 20 seconds |

How to play:

1. Wait through the round's preparation countdown.
2. Watch the numbered **3 × 3 grid** in the group. One tile is highlighted at a time; remember the order.
3. When prompted, open the bot's private chat. The numbered controls appear after a short ready phase.
4. Tap the numbers **1–9** in the exact order shown.
5. One incorrect tile breaks the candy and eliminates that player. Players who do not complete the sequence before the input timer expires are also eliminated.
6. Players who clear the pattern survive this round and can enter the next one.

The sequence is generated for the selected shape at runtime. A longer sequence is more difficult because it requires more correct taps within the input window.

### Round 3 — 🎱 Marbles: Odd or Even

**Objective:** Win your paired match by taking your opponent's marbles.

- The bot randomly shuffles the surviving players into pairs. If there is an odd number of players, one player receives a **bye** and advances.
- Each player starts with **10 marbles**.
- One player in a pair is assigned as the **Hider**; the other is the **Guesser**.
- The Hider privately chooses how many marbles to hide, from 1 up to their current amount.
- The Guesser places a bet and then privately predicts whether the hidden amount is **ODD** or **EVEN**.
- A correct guess transfers the bet from the Hider to the Guesser. An incorrect guess transfers the bet from the Guesser to the Hider.
- The roles switch for the next exchange while both players still have marbles.
- When a player's marble count reaches zero, that player loses the pair and the opponent advances.
- The round has a **180-second** time limit. If a pair has not finished when time expires, both players in that unfinished pair are eliminated.

The group round card shows pair status and results; the private chat handles hiding, betting, and guessing.

### Round 4 — 🌉 Glass Bridge

**Objective:** Cross a 20-step bridge without choosing a fragile glass panel.

- The bridge contains **20 rows**, each with a left and right panel. One panel per row is randomly safe; the other breaks.
- The order of surviving players is randomized.
- Only the player whose turn it is receives the **LEFT GLASS** and **RIGHT GLASS** controls in their DM.
- A correct choice confirms that step and advances the shared bridge progress. If the player chooses the wrong panel, they fall and are eliminated; the next player tries the next unresolved step.
- A player who safely completes all 20 steps qualifies from the Bridge.
- The round has a **300-second** time limit. Anyone who has not crossed the complete bridge when time expires is eliminated.

The group board shows the bridge, the current player/step, broken panels, and the number of players who have crossed. The board's confirmed progress is shared, so later players can continue from the last safely confirmed step rather than restarting the bridge from step one.

### Conditional Round 5 — 🗳 Elimination Vote

**Objective:** Survive the vote and continue to the Final.

This round runs only when the number of Glass Bridge qualifiers who actually enter the next round is odd (and at least two players enter). Otherwise, the match skips directly to the Final.

- Each eligible player receives a private ballot and may vote for **one other living player**. Self-voting is not allowed, and each player can vote only once.
- The group announcement reports how many votes have been submitted, not the ballot choices.
- The player with the most votes is eliminated.
- If the highest vote count is tied, the bot randomly selects one of the tied players for elimination.
- The voting window is **60 seconds**. Voting resolves early once all living eligible players have voted. If the timer expires with no votes submitted, a random living player is eliminated.
- The remaining players may enter the Squid Game Final.

### Final Round — ⚔️ Squid Game: Tactical Duels

**Objective:** Win every required duel and become the last player standing.

The Final is a bracket tournament. Players are randomly paired for each bracket, and the winners advance. If a bracket has an odd number of contenders, one player receives a bye. Between brackets, advancing players must press **ENTER NEXT ROUND** before the entry countdown closes.

Each duel uses three combat choices:

| Move | Beats |
|---|---|
| ⚔️ Offensive | 🛡️ Defensive |
| 🛡️ Defensive | 🃏 Trickster |
| 🃏 Trickster | ⚔️ Offensive |

- Each duelist starts with **100 HP**.
- Players choose privately. A choice stays hidden until both duelists have picked.
- If both choose the same move, neither takes damage.
- Otherwise, the losing move takes **10 damage**. Exchanges repeat until one player's HP reaches zero; the other duelist advances.
- Each bracket has a **60-second** time limit. If only one player has picked when time expires, that player advances by default. If neither has picked, the bot randomly decides the duel.
- Winners of a bracket enter the next bracket until a single champion remains.

The bot announces the champion in the group and increments that user's win count in MongoDB.

## Getting started

### Requirements

- Python **3.11 or newer**
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- A reachable MongoDB instance (local MongoDB or MongoDB Atlas)
- A group where the bot can send messages and edit its own game messages

### 1. Install the dependencies

From the repository root:

```bash
python -m venv .venv
```

Activate the environment:

```bash
# Linux / macOS
source .venv/bin/activate

# Windows PowerShell (run this instead of the previous line)
.venv\Scripts\Activate.ps1
```

Install the project's pinned dependencies:

```bash
python -m pip install --upgrade pip
pip install -r requirements.txt
```

### 2. Configure environment variables

Create a file named `.env` in the project root (the same directory as `main.py`):

```dotenv
BOT_TOKEN=replace_with_your_telegram_bot_token
MONGO_URI=mongodb://localhost:27017
DB_NAME=squid_game_bot
LOG_LEVEL=INFO
WELCOME_VIDEO=assets/welcome.mp4
```

`BOT_TOKEN` is required. The other variables have defaults; see [Configuration](#configuration). **Do not commit `.env` or share your real bot token.** The repository's `.gitignore` excludes `.env` files.

### 3. Start MongoDB

If MongoDB is installed locally, start its service first. For a local development database with Docker, you can use:

```bash
docker run -d --name squid-game-mongo -p 27017:27017 mongo:7
```

Alternatively, use a MongoDB Atlas connection string by setting `MONGO_URI` in `.env`. Make sure the hosting machine can reach the MongoDB server and that any credentials in the URI are URL-encoded where required.

### 4. Run the bot

```bash
python main.py
```

On startup, the bot checks/creates the required MongoDB indexes, restores lobby timers and pending next-round entry gates, and cleans up interrupted active mini-games. It also attempts to post a startup notification containing the latest Git commit subject to the updates channel configured in `main.py`. A notification failure is logged and does not intentionally block startup.

### 5. Add the bot to a group and play

1. Add the bot to your Telegram group.
2. Each person—including the host if they want to play—opens the bot in a private chat and sends `/start`.
3. In the group, run `/enterthehell`.
4. Everyone taps **🙋 Join** on the lobby card.
5. After the minimum player count is reached, the host can select **▶️ Start Now**, or wait for the lobby timer to auto-start the match.
6. Follow each group announcement and private prompt. After qualifying, return to the group and tap **✅ ENTER NEXT ROUND** before its countdown ends.

## Commands

| Command | Where | Purpose |
|---|---|---|
| `/start` | Private chat | Opens the bot, registers/updates the user profile, and displays the welcome message/video. |
| `/enterthehell` | Group | Creates a new lobby. The group cannot run another active match while one is in progress. |
| `/cancelgame` | Group | Cancels an active match. Available to the host or a group administrator (the configured bot-admin account is also allowed). |
| `/stats` | Group or private chat | Displays total games, active games, tracked users, and tracked groups. |
| `/help` | Group or private chat | Shows commands and a button to open the game guide. |
| `/report <details>` | Any chat | Sends a bug report to the configured bot-admin account. You can also attach supported media and put `/report` plus details in its caption. |

## Configuration

Environment variables are read by `bot/config.py` using `python-dotenv`.

| Variable | Default | Description |
|---|---|---|
| `BOT_TOKEN` | *(required)* | Telegram bot token from @BotFather. |
| `MONGO_URI` | `mongodb://localhost:27017` | MongoDB connection URI. |
| `DB_NAME` | `squid_game_bot` | Database name. |
| `DB_MAX_POOL_SIZE` | `50` | Maximum MongoDB client connection pool size (the code enforces a minimum of 5). |
| `DB_MIN_POOL_SIZE` | `0` | Minimum MongoDB client connection pool size, clamped between 0 and the maximum. |
| `LOG_LEVEL` | `INFO` | Python logging level, such as `INFO` or `DEBUG`. |
| `WELCOME_VIDEO` | `assets/welcome.mp4` | Local welcome video path. If unavailable, `/start` falls back to the text welcome message. |
| `MIN_PLAYERS` | `2` | Minimum lobby size; the code enforces a minimum of 2. |
| `MAX_PLAYERS` | `20` | Maximum players per match. This is a code constant, not an environment override. |
| `LOBBY_SECONDS` | `60` | Lobby countdown in seconds; the code enforces a minimum of 10. |
| `ROUND_ENTRY_SECONDS` | `30` | Time qualified players have to enter the next round; the code enforces a minimum of 5. |

The mini-game timers currently defined in code are:

| Round | Time limit / input window |
|---|---|
| Red Light, Green Light | 90 seconds for the race |
| Dalgona | Shape-dependent sequence input window (approximately 11, 14, 17, or 20 seconds) |
| Marbles | 180 seconds |
| Glass Bridge | 300 seconds |
| Elimination Vote | 60 seconds |
| Squid Game Final | 60 seconds per bracket round |

Several rounds also show a preparation countdown before private controls open. Some timings are constants in the game modules rather than environment settings; changing a value in `.env` will not override those hard-coded round timers.

## How the code is organized

```text
squid-game-bot/
├── main.py                 # Telegram application, handlers, startup and health server
├── requirements.txt        # Pinned Python dependencies
├── Procfile                # Hosting process command
├── stackhost.yaml          # Hosting configuration example
├── assets/
│   └── welcome.mp4         # Optional welcome video asset
└── bot/
    ├── config.py           # Environment variables and game settings
    ├── database.py         # MongoDB client, collections and indexes
    ├── repository.py       # User/match persistence and database helpers
    ├── manager.py          # Round transitions, next-round gates, results and champion logic
    ├── runtime.py          # Process-local locks, timers and active game coordination
    ├── ui.py               # Lobby, round cards, keyboards and game-board rendering
    ├── handlers/
    │   └── lobby.py        # /start, lobby, joining, cancellation, stats, help and reports
    └── games/
        ├── rlgl.py         # Red Light, Green Light
        ├── dalgona.py      # Dalgona sequence memory challenge
        ├── marbles.py      # Pair-based odd/even marble game
        ├── bridge.py       # Shared 20-step Glass Bridge
        ├── voting.py       # Conditional elimination vote
        └── final.py        # Tactical duel brackets
```

### Main responsibilities

- **`main.py`** builds the `python-telegram-bot` application, registers command/callback handlers, starts polling, and serves `/` and `/health` on the configured `PORT` (default `10000`).
- **`manager.py`** is the match state machine. It records which players qualified/eliminated, creates the next-round entry gate, decides whether the conditional Vote is needed, starts the next game, and crowns the champion.
- **`repository.py` / `database.py`** centralize MongoDB reads/writes and indexes. Core collections include users, matches, welcome assets, bot statistics, and tracked groups.
- **`runtime.py`** holds in-process asyncio locks, queues, timers, and game coordination objects.
- **`ui.py`** creates shared group announcements, player scoreboards, round-entry buttons, and private mini-game controls.
- **`bot/games/`** keeps each mini-game's rules and callback handlers separate.

## Persistence, hosting, and recovery

- Match records, player details, round results, user profiles, and champion win counts are stored in MongoDB.
- Lobby deadlines and pending next-round entry gates are restored during startup when their records are still active.
- **An active mini-game is not fully resumable after a process restart.** Its timers, locks, and some live state are process-local. On startup, `cleanup_interrupted_games()` marks active mini-game matches as terminated and asks players to start a new lobby. Plan deployments/restarts accordingly.
- Run **one bot process/replica** for this version. The active game coordinator uses in-memory asyncio state; running multiple replicas against the same database is not a supported horizontal-scaling setup.
- Finished match records are scheduled for cleanup after a short delay, so the final messages can be posted. User records and win counts are stored separately.
- The app starts a lightweight HTTP server on `PORT` (default `10000`). `GET /health` returns `200 OK`; `GET /` returns a simple running status.
- The startup notification channel ID is currently hard-coded in `main.py`. The bot must be able to post to that channel. Change the ID in the code if your deployment uses another channel.
- **Deployment environment name:** the Python code reads `MONGO_URI`. The current `stackhost.yaml` contains a `MONGODB_URI` mapping, which does not match the code. Set `MONGO_URI` in the hosting dashboard or correct the mapping before deploying; otherwise the app may fall back to its local MongoDB default.

## Troubleshooting

### Players cannot join or do not receive private game controls

Ask each player to open the bot and send `/start` first. If the bot still cannot DM a player, that player may be eliminated or unable to participate in a DM-driven round.

### The lobby closes without starting

The lobby auto-starts only when at least `MIN_PLAYERS` have joined before the countdown expires. The host can use **Start Now** only after the minimum is met.

### There is no next round

Surviving does not automatically register a player for the next round. They must tap **ENTER NEXT ROUND** before the entry countdown ends. Players who miss it are removed from the match.

### MongoDB connection errors

Check that MongoDB is running and reachable, `MONGO_URI` is correct, the credentials/database permissions are valid, and your hosting provider has access to the database network. Do not confuse `MONGO_URI` with `MONGODB_URI`; this code reads the former.

### Health checks fail on a hosting platform

Confirm that the platform exposes/uses the same `PORT` environment variable the app binds to and that the service type permits the process to open that port. The health route is `/health`.

### Rich cards or group updates fail

Ensure the bot can send messages to the group. Avoid running multiple app instances, and check the logs for Telegram API errors or flood-control waits. The bot logs a startup-notification failure without intending to stop the game process.

- **Glass Bridge validation note:** the current source advances through the player queue after a player crosses the final tile, even though the shared track has no further tile. Test/fix that last-tile path before relying on multiple players crossing the same completed bridge in one match.

---

This README describes the behavior implemented in the current source tree. If round rules or timer constants change in `bot/config.py` or `bot/games/`, update the corresponding sections here as well.
