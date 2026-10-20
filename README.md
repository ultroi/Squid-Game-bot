# 🦑 Squid Game Bot (Telegram)

A full survival-game bot for Telegram groups — python-telegram-bot v21 (async) + MongoDB (Motor).

## Games
1. **🔴🟢 Red Light, Green Light** — 10×10 live grid in the group; players move with DM buttons. The light is ONLY shown in the group message. Moving on red = eliminated. 60s to cross.
2. **🍪 Dalgona** — random shape (⭕🔺⭐☂️); harder shape = more memory-sequence rounds on a 3×3 grid. 120s.
3. **🎱 Marbles** — pairs play odd/even with 10 marbles each; first to hold all 20 survives. 180s or both die.
4. **🌉 Glass Bridge** — 2×20 grid, random queue, next player continues from the last confirmed tile. 300s or everyone dies.
5. **🗳 Elimination Vote** — only if an ODD number survives the bridge; anonymous DM vote.
6. **🦑 Squid Game final** — PvP duels, 100 HP, ⚔️/🛡️/🃏 triangle, 10 damage per hit, bracket until one champion.

## Setup
```bash
pip install -r requirements.txt
cp .env.example .env          # paste BOT_TOKEN from @BotFather
# put your ~7MB welcome clip at assets/welcome.mp4 (optional, text fallback exists)
python main.py
```
MongoDB: local install, `docker run -d -p 27017:27017 mongo:7`, or a free Atlas URI in `MONGO_URI`.

## Usage
1. Add the bot to a group.
2. Every player opens the bot in DM and presses **/start** (mandatory — games are played in DM).
3. In the group: **/enterthehell** → players tap **🙋 JOIN GAME** (5–20 players).
4. Host taps **▶️ START NOW** or wait for auto-start.

## Architecture notes
- One active match per group; match + game state persisted in MongoDB (`matches` collection), so panels survive edits safely and callbacks stay resumable.
- Hot timing state (RLGL light, asyncio timers/locks) lives in `bot/runtime.py` in memory — single-process by design. For multi-instance scaling you'd move timers to Redis/Celery.
- Callback data is namespaced per game (`rlgl:`, `dal:`, `mar:`, `bri:`, `vot:`, `fin:`) with the match id embedded, so concurrent groups never collide.
- `manager.py` is the stage machine: lobby → rlgl → dalgona → marbles → bridge → [vote if odd] → final → champion.
