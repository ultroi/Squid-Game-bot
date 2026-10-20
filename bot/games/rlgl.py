import asyncio
import logging
import math
import random
import time

from telegram.error import BadRequest, RetryAfter

from .. import config, runtime, ui
from ..manager import safe_answer, game_finished, refresh_lobby_scoreboard
from ..repository import get_match, save_match, alive, save_rlgl_players

log = logging.getLogger(__name__)

# ---------------------------------------------------------------- tunables

# Global edit budget (Telegram allows ~30 msg/s overall; stay below it).
EDIT_RATE_PER_SEC = 20.0
EDIT_BURST = 10
_UI_IO_SEMAPHORE = asyncio.Semaphore(16)

# Per-DM refresh intervals.
MOVER_INTERVAL = 0.6            # the player who is actively moving
OTHER_INTERVAL_MIN = 1.2        # other racers see the board at most this often
OTHER_PLAYERS_PER_SEC = 12.0    # ...scaled up so N players cost <= 12 edits/s
SPECTATOR_INTERVAL_MIN = 3.0    # eliminated / finished players
SPECTATOR_PLAYERS_PER_SEC = 6.0

RENDER_POLL_SECONDS = 0.25
HOUSEKEEPING_SECONDS = 0.5
TIMER_REFRESH_SECONDS = 2.0
PERSIST_SECONDS = 2.5

GROUP_THROTTLE_SECONDS = 3.0    # group chats are limited to ~20 edits/min
GROUP_PERIODIC_SECONDS = 6.0

# Phase grace: a player is judged by the light *they were shown*.
STALE_PHASE_GRACE_SECONDS = 1.5
MAX_STALE_PHASE_AGE_SECONDS = 3.0

# Tap handling.
MOVE_COOLDOWN = 0.12
MOVE_QUEUE_MAX = 400
MOVE_ACK_TIMEOUT = 1.0

URGENT, MOVER, OTHER = 0, 1, 2


# ---------------------------------------------------------------- rate limiter

class _RateLimiter:
    """Token-bucket style limiter (calls per second), safe for one event loop."""

    def __init__(self, rate, burst):
        self._rate = float(rate)
        self._burst = max(1, int(burst))
        self._next = 0.0

    async def acquire(self):
        now = time.monotonic()
        # Idle time earns up to `burst` free slots.
        self._next = max(self._next, now - self._burst / self._rate)
        wait = self._next - now
        self._next += 1.0 / self._rate
        if wait > 0:
            await asyncio.sleep(wait)


_EDIT_LIMITER = _RateLimiter(EDIT_RATE_PER_SEC, EDIT_BURST)


# ---------------------------------------------------------------- pure helpers

def _seconds_left(match):
    game_data = match.get("game") or {}
    started = game_data.get("started_at")
    duration = game_data.get("duration")
    if started is None or duration is None:
        return None
    return max(0, math.ceil(started + duration - time.time()))


def _message_id(result):
    if isinstance(result, dict):
        return result.get("message_id")
    return getattr(result, "message_id", None)


def _uid_key(user_id):
    return str(user_id)


def _retry_seconds(exc):
    delay = getattr(exc, "retry_after", 1)
    if hasattr(delay, "total_seconds"):
        delay = delay.total_seconds()
    return max(1.0, float(delay))


def _hydrate_grid(match):
    """Snapshot the player positions into a uid-keyed dict for the tick loop."""
    size = max(1, int(config.GRID_SIZE))
    grid = {}
    for index, player in enumerate(match.get("players") or []):
        if player.get("id") is None:
            continue
        uid = str(player.get("id"))
        pos = player.get("pos")
        try:
            row, col = int(pos[0]), int(pos[1])
        except (TypeError, ValueError, IndexError):
            row = size - 1 - (index // size)
            col = index % size
        if not (0 <= row < size and 0 <= col < size):
            row, col = size - 1, size // 2
        grid[uid] = {
            "pos": [row, col],
            "alive": bool(player.get("alive", True)),
            "reached": bool(player.get("reached", False)),
            "name": player.get("name"),
            "emoji": player.get("emoji"),
        }
    return grid


def _merge_grid_into_match(match, grid):
    """Write grid state back to the match's players array (keeps dm_mid etc.)."""
    for player in match.get("players", []):
        g = grid.get(str(player.get("id")))
        if not g:
            continue
        player["pos"] = list(g["pos"])
        player["alive"] = g["alive"]
        player["reached"] = g["reached"]
    return match


def _group_details(match, light=None):
    """Simplified group details showing only total time and finish status."""
    game_data = match.get("game") or {}
    duration = game_data.get("duration", config.RLGL_SECONDS)
    
    # Check if the round is already finished
    if match.get("round_result_message_id") or game_data.get("round_finished"):
        return f"⏱ <b>Total Time:</b> {duration}s\n🛑 <b>Race Finished!</b>\n"
        
    return f"⏱ <b>Total Time:</b> {duration}s\n"

def _is_racing(g):
    return bool(g) and g.get("alive") and not g.get("reached")


def _mark_dirty(rt, uid, prio):
    """Mark one DM for re-render; keeps the most urgent priority and wakes the worker."""
    dirty = rt.setdefault("dirty", {})
    current = dirty.get(uid)
    if current is None or prio < current:
        dirty[uid] = prio
    event = rt.get("render_event")
    if event is not None:
        event.set()


def _mark_all(rt, racing_prio, spectator_prio):
    for uid, g in (rt.get("grid") or {}).items():
        _mark_dirty(rt, uid, racing_prio if _is_racing(g) else spectator_prio)


def _resolve_move(move, text, alert=False):
    fut = move.get("fut")
    if fut is not None and not fut.done():
        fut.set_result((text, alert))


# ---------------------------------------------------------------- runtime init

def _initialize_round_runtime(rt, match=None):
    rt.update({
        "light": "green",
        "done": False,
        "round_finalizing": False,
        "phase_seq": 0,
        "phase_token": "0",
        "phase_history": {"0": {"light": "green", "created_at": time.monotonic()}},
        "player_phase_tokens": {},
        "player_grace_tokens": {},
        "player_phase_sync_failed": {},
        "player_rendered_version": {},
        "dm_locks": {},
        "dm_blocked_until": {},
        "last_dm_refresh": {},
        "last_move_at": {},
        "last_group_refresh": 0.0,
        "state_version": 1,
        "grid": _hydrate_grid(match) if match else {},
        "grid_version": 0,
        "match_snapshot": match,
        "dirty": {},
        "dirty_dms": set(),          # legacy key, unused
        "render_event": asyncio.Event(),
        "move_queue": asyncio.Queue(maxsize=MOVE_QUEUE_MAX),
        "group_dirty": False,
        "persist_dirty": False,
        "persist_lock": asyncio.Lock(),
        "bot_username": None,
    })


def _dm_lock(rt, user_id):
    return rt.setdefault("dm_locks", {}).setdefault(_uid_key(user_id), asyncio.Lock())


async def _open_dm_markup(context, rt):
    """Inline 'Open Bot DM' keyboard; get_me() is only called once per round."""
    username = rt.get("bot_username")
    if not username:
        try:
            username = (await context.bot.get_me()).username
            rt["bot_username"] = username
        except Exception:
            return ui.kb([])
    return ui.open_dm_keyboard(username)


async def _prewarm_images():
    """Warm the resizer cache so the first red flip does not wait on a cold fetch."""
    try:
        import httpx
    except ImportError:
        return
    urls = []
    for light in ("green", "red"):
        try:
            urls.append(ui.rlgl_image_url(light))
        except Exception:
            pass
    if not urls:
        return
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            await asyncio.gather(*(client.get(u) for u in urls), return_exceptions=True)
    except Exception:
        log.debug("RLGL image prewarm failed", exc_info=True)


# ---------------------------------------------------------------- group card

async def _refresh_group_round(context, mid, light=None, event=None, finished=False,
                               reply_markup=None, force=False):
    rt = runtime.get(mid)
    now = time.monotonic()
    last = rt.get("last_group_refresh", 0)
    if not force and not finished and (now - last < GROUP_THROTTLE_SECONDS):
        return
    async with rt.setdefault("group_refresh_lock", asyncio.Lock()):
        snapshot = rt.get("match_snapshot") or await get_match(mid)
        if not snapshot or (snapshot.get("stage") != "rlgl" and not finished):
            return
        if not finished and (rt.get("done") or (snapshot.get("game") or {}).get("round_finished")):
            return
        display_event = event if event is not None else (snapshot.get("game") or {}).get("last_event")
        if not finished and reply_markup is None:
            reply_markup = await _open_dm_markup(context, rt)
        elif finished:
            reply_markup = ui.kb([])
        await ui.refresh_round_announcement(
            context.bot, snapshot, "rlgl",
            details=_group_details(snapshot, light),
            event=display_event, finished=finished, reply_markup=reply_markup,
        )
        rt["last_group_refresh"] = time.monotonic()
    await refresh_lobby_scoreboard(context, mid)


# ---------------------------------------------------------------- begin

async def begin(context, mid):
    match = await get_match(mid)
    if not match:
        return
    players = alive(match)
    if not players:
        return

    size = max(1, int(config.GRID_SIZE))
    for index, player in enumerate(players):
        player["pos"] = [size - 1 - (index // size), index % size]
        player["reached"] = False
        player.pop("dm_mid", None)
        player.pop("dm_image_mid", None)

    match["game"] = {
        "started_at": None,
        "duration": config.RLGL_SECONDS,
        "last_event": "The race is getting ready.",
        "round_finished": False,
    }
    await save_match(match)

    rt = runtime.get(mid)
    async with rt["lock"]:
        _initialize_round_runtime(rt, match)
        initial_token = rt["phase_token"]
        initial_version = rt["state_version"]

    runtime.add_task(mid, asyncio.create_task(_prewarm_images()))

    await refresh_lobby_scoreboard(context, mid)
    dm_markup = await _open_dm_markup(context, rt)

    await ui.send_round_announcement(
        context.bot, match, "rlgl",
        details="⏳ <b>Private race starts in 5 seconds</b>",
        event="The race is getting ready.",
    )
    await save_match(match)

    for remaining in (4, 3, 2, 1):
        await asyncio.sleep(1)
        latest = await get_match(mid)
        if not latest or latest.get("stage") != "rlgl":
            return
        await ui.refresh_round_announcement(
            context.bot, latest, "rlgl",
            details=f"⏳ <b>Private race starts in {remaining} seconds</b>",
            event="The race is getting ready.",
            reply_markup=ui.kb([]),
        )

    await asyncio.sleep(1)
    match = await get_match(mid)
    if not match or match.get("stage") != "rlgl":
        return
    players = alive(match)

    await ui.refresh_round_announcement(
        context.bot, match, "rlgl",
        details="📩 <b>Opening private race controls…</b>",
        event="Preparing private race controls.",
        reply_markup=ui.kb([]),
    )

    # Every board is rendered from the same snapshot, so build the shared part once.
    start_ctx = ui.rlgl_render_context(match, "green")

    async def send_player_board(player):
        user_id = player.get("id")
        rich_html = ui.rlgl_dm_html(
            match, user_id, "green", config.RLGL_SECONDS,
            match_id=mid, phase_token=initial_token,
            image_url=match.get("rlgl_image_url"), ctx=start_ctx,
        )
        for attempt in range(3):
            try:
                await _EDIT_LIMITER.acquire()
                async with _UI_IO_SEMAPHORE:
                    result = await ui.send_rlgl_rich_message(context.bot, user_id, rich_html)
                message_id = _message_id(result)
                if not message_id:
                    raise RuntimeError("No message_id")
                player["dm_mid"] = message_id
                rt["player_phase_tokens"][_uid_key(user_id)] = initial_token
                rt["player_rendered_version"][_uid_key(user_id)] = initial_version
                rt["last_dm_refresh"][_uid_key(user_id)] = time.monotonic()
                return True
            except RetryAfter as exc:
                await asyncio.sleep(_retry_seconds(exc) + 0.2)
            except Exception:
                if attempt == 2:
                    break
                await asyncio.sleep(0.5)
        player["alive"] = False
        return False

    await asyncio.gather(*(send_player_board(p) for p in players))

    unreachable = [p for p in players if not p.get("dm_mid")]
    start_event = "The race has started."
    if unreachable:
        names = ", ".join(str(p.get("name") or p.get("id")) for p in unreachable[:3])
        extra = f" +{len(unreachable) - 3}" if len(unreachable) > 3 else ""
        start_event = f"The race has started. DM unreachable, disqualified: {names}{extra}"

    async with rt["lock"]:
        rt["grid"] = _hydrate_grid(match)
        rt["match_snapshot"] = match
        match["game"]["started_at"] = time.time()
        match["game"]["last_event"] = start_event
        match["round_preparing"] = False
        rt["state_version"] += 1
        await save_match(match)

    await refresh_lobby_scoreboard(context, mid, force=True)
    await _refresh_group_round(
        context, mid, "green", event=start_event,
        reply_markup=dm_markup, force=True,
    )

    if not any(_is_racing(g) for g in rt["grid"].values()):
        # Nobody could be reached; do not wait for the timer.
        await _finish_round(context, mid, [], timed_out=True)
        return

    for coro in (
        _light_loop(context, mid),
        _housekeeping_loop(context, mid),
        _move_tick_loop(context, mid),
        _render_worker(context, mid),
        _persist_loop(context, mid),
    ):
        runtime.add_task(mid, asyncio.create_task(coro))


# ---------------------------------------------------------------- move tick (memory only)

async def _move_tick_loop(context, mid):
    rt = runtime.get(mid)
    q = rt["move_queue"]
    try:
        while not rt.get("done"):
            try:
                first = await asyncio.wait_for(q.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            moves = [first]
            while True:
                try:
                    moves.append(q.get_nowait())
                except asyncio.QueueEmpty:
                    break
            try:
                await _apply_moves(context, mid, moves)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("rlgl move batch failed (mid=%s)", mid)
                for move in moves:
                    _resolve_move(move, None)
    except asyncio.CancelledError:
        return
    finally:
        while True:
            try:
                _resolve_move(q.get_nowait(), "The round is over.")
            except asyncio.QueueEmpty:
                break


async def _apply_moves(context, mid, moves):
    rt = runtime.get(mid)
    snapshot = rt.get("match_snapshot")
    if (
        rt.get("done") or rt.get("round_finalizing") or not snapshot
        or not (snapshot.get("game") or {}).get("started_at")
    ):
        for move in moves:
            _resolve_move(move, "The round is not active.")
        return

    race_ended = False
    qualifiers = []
    last_event = None
    state_changed = False
    touched = False

    async with rt["lock"]:
        if rt.get("done") or rt.get("round_finalizing"):
            for move in moves:
                _resolve_move(move, "The round is finishing.")
            return

        grid = rt.get("grid") or {}
        size = max(1, int(config.GRID_SIZE))

        for move in moves:
            uid = str(move["uid"])
            g = grid.get(uid)
            if not g:
                _resolve_move(move, "You are not in this race.", True)
                continue
            if not g["alive"]:
                _resolve_move(move, "❌ You are eliminated.")
                continue
            if g["reached"]:
                _resolve_move(move, "🏁 You already finished.")
                continue

            status, phase_light = _resolve_button_phase(rt, move["uid"], move["token"])
            if status != "ok":
                _mark_dirty(rt, uid, URGENT)
                _resolve_move(move, "⚠️ Controls were out of date — refreshing.")
                continue

            if phase_light == "red":
                g["alive"] = False
                last_event = (
                    f"{g.get('emoji') or '👤'} {g.get('name') or 'A player'} "
                    "moved on red and was eliminated."
                )
                _mark_dirty(rt, uid, URGENT)
                rt["group_dirty"] = True
                state_changed = True
                touched = True
                _resolve_move(move, "💀 You moved on red!", True)
                continue

            row, col = g["pos"]
            if move["direction"] == "up":
                new_row, new_col = max(0, row - 1), col
            elif move["direction"] == "left":
                new_row, new_col = row, max(0, col - 1)
            else:
                new_row, new_col = row, min(size - 1, col + 1)

            if (new_row, new_col) == (row, col):
                _resolve_move(move, "⛔ Edge of the track.")
                continue

            g["pos"] = [new_row, new_col]
            touched = True
            if new_row == 0:
                g["reached"] = True
                last_event = (
                    f"{g.get('emoji') or '👤'} {g.get('name') or 'A player'} "
                    "reached the finish line."
                )
                rt["group_dirty"] = True
                state_changed = True
                _mark_dirty(rt, uid, URGENT)
                _resolve_move(move, "🏁 You reached the finish!")
            else:
                _mark_dirty(rt, uid, MOVER)
                _resolve_move(
                    move, f"📍 {new_row} step{'s' if new_row != 1 else ''} to go"
                )

        if touched:
            rt["state_version"] += 1
            rt["grid_version"] += 1
            rt["persist_dirty"] = True
            # Everyone else sees the new grid, but only at their own (slow) interval.
            for u, g in grid.items():
                if _is_racing(g):
                    _mark_dirty(rt, u, OTHER)
                elif state_changed:
                    _mark_dirty(rt, u, OTHER)

            _merge_grid_into_match(snapshot, grid)
            if last_event:
                snapshot.setdefault("game", {})["last_event"] = last_event

            if not any(_is_racing(g) for g in grid.values()):
                race_ended = True
                qualifiers = [int(u) for u, g in grid.items() if g["alive"] and g["reached"]]

    if race_ended:
        await _finish_round(context, mid, qualifiers)


# ---------------------------------------------------------------- render worker (all DM I/O)

async def _render_worker(context, mid):
    rt = runtime.get(mid)
    event = rt["render_event"]
    try:
        while not rt.get("done"):
            try:
                await asyncio.wait_for(event.wait(), timeout=RENDER_POLL_SECONDS)
            except asyncio.TimeoutError:
                pass
            event.clear()
            if rt.get("done"):
                return
            try:
                await _render_batch(context, mid, rt)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("rlgl render batch failed (mid=%s)", mid)
    except asyncio.CancelledError:
        return


async def _render_batch(context, mid, rt):
    dirty = rt.get("dirty")
    if not dirty:
        return
    snapshot = rt.get("match_snapshot")
    if not snapshot:
        dirty.clear()
        return

    now = time.monotonic()
    grid = rt.get("grid") or {}
    total = max(1, len(grid))
    other_interval = max(OTHER_INTERVAL_MIN, total / OTHER_PLAYERS_PER_SEC)
    spectator_interval = max(SPECTATOR_INTERVAL_MIN, total / SPECTATOR_PLAYERS_PER_SEC)
    blocked = rt.setdefault("dm_blocked_until", {})
    last_refresh = rt.setdefault("last_dm_refresh", {})

    due = []
    for uid, prio in list(dirty.items()):
        if blocked.get(uid, 0) > now:
            continue  # per-chat flood wait still active; stays dirty
        if prio != URGENT:
            if prio == MOVER:
                interval = MOVER_INTERVAL
            else:
                interval = other_interval if _is_racing(grid.get(uid)) else spectator_interval
            if now - last_refresh.get(uid, 0.0) < interval:
                continue
        due.append((prio, uid))
    if not due:
        return

    due.sort()
    for _, uid in due:
        dirty.pop(uid, None)

    # Everything below up to the send is synchronous: one consistent view of state.
    light = rt.get("light", "green")
    token = str(rt.get("phase_token", "0"))
    version = int(rt.get("state_version", 0))
    seconds_left = _seconds_left(snapshot)
    ctx = ui.rlgl_render_context(snapshot, light)
    by_uid = {str(p.get("id")): p for p in snapshot.get("players", [])}
    image_url = snapshot.get("rlgl_image_url")

    jobs = []
    for prio, uid in due:
        player = by_uid.get(uid)
        if not player or not player.get("dm_mid"):
            continue
        rich_html = ui.rlgl_dm_html(
            snapshot, player["id"], light, seconds_left,
            match_id=mid, phase_token=token, image_url=image_url, ctx=ctx,
        )
        jobs.append((prio, uid, _send_dm_edit(
            context, mid, player["id"], player["dm_mid"], rich_html, token, version,
        )))

    if not jobs:
        return
    results = await asyncio.gather(*(job for _, _, job in jobs), return_exceptions=True)
    for (prio, uid, _), result in zip(jobs, results):
        if result == "retry":
            _mark_dirty(rt, uid, prio)


async def _send_dm_edit(context, mid, user_id, message_id, rich_html,
                        phase_token, render_version, *, final=False):
    """One DM edit: rate limited, serialised per player, flood-aware.

    Returns "ok", "skipped", "retry" or "failed".
    """
    rt = runtime.get(mid)
    uid = _uid_key(user_id)
    async with _dm_lock(rt, user_id):
        if rt.get("done") and not final:
            return "skipped"
        if not final and render_version < rt.setdefault("player_rendered_version", {}).get(uid, -1):
            return "skipped"

        outcome = "failed"
        for _ in range(3 if final else 1):
            await _EDIT_LIMITER.acquire()
            try:
                async with _UI_IO_SEMAPHORE:
                    await ui.edit_rlgl_rich_message(context.bot, user_id, message_id, rich_html)
                outcome = "ok"
                break
            except BadRequest as exc:
                if "not modified" in str(exc).lower():
                    outcome = "ok"  # the message already shows this content
                else:
                    log.warning("RLGL DM edit failed (mid=%s uid=%s): %s", mid, uid, exc)
                    outcome = "failed"
                break
            except RetryAfter as exc:
                seconds = _retry_seconds(exc) + 0.2
                rt.setdefault("dm_blocked_until", {})[uid] = time.monotonic() + seconds
                outcome = "retry"
                if final:
                    await asyncio.sleep(seconds)
                    continue
                break
            except Exception:
                log.debug("RLGL DM edit error (mid=%s uid=%s)", mid, uid, exc_info=True)
                outcome = "failed"
                break

        if outcome != "ok":
            if outcome == "failed":
                rt.setdefault("player_phase_sync_failed", {})[uid] = str(phase_token)
            return outcome

        now_time = time.monotonic()
        rt.setdefault("last_dm_refresh", {})[uid] = now_time
        if rt.setdefault("player_phase_sync_failed", {}).get(uid) == str(phase_token):
            rt["player_phase_sync_failed"].pop(uid, None)

        previous_token = rt.setdefault("player_phase_tokens", {}).get(uid)
        grace = rt.setdefault("player_grace_tokens", {}).setdefault(uid, {})
        if previous_token and previous_token != str(phase_token):
            grace[previous_token] = now_time + STALE_PHASE_GRACE_SECONDS
        for token, expiry in list(grace.items()):
            if expiry <= now_time:
                grace.pop(token, None)

        rt["player_phase_tokens"][uid] = str(phase_token)
        rt["player_rendered_version"][uid] = max(
            render_version, rt["player_rendered_version"].get(uid, -1)
        )
        return "ok"


async def _refresh_all_dms_final(context, mid, snapshot, light, token, version):
    """Last board for every player (finished / eliminated / racing)."""
    rt = runtime.get(mid)
    ctx = ui.rlgl_render_context(snapshot, light)
    seconds_left = _seconds_left(snapshot)
    jobs = []
    for player in snapshot.get("players", []):
        if not player.get("dm_mid"):
            continue
        rich_html = ui.rlgl_dm_html(
            snapshot, player["id"], light, seconds_left,
            match_id=mid, round_finished=True, phase_token=token,
            image_url=snapshot.get("rlgl_image_url"), ctx=ctx,
        )
        jobs.append(_send_dm_edit(
            context, mid, player["id"], player["dm_mid"], rich_html,
            token, version, final=True,
        ))
    if jobs:
        await asyncio.gather(*jobs, return_exceptions=True)
    rt["dirty"] = {}


# ---------------------------------------------------------------- persistence (debounced)

async def _persist_players(mid, rt):
    async with rt.setdefault("persist_lock", asyncio.Lock()):
        if rt.get("done") or not rt.get("persist_dirty"):
            return
        snapshot = rt.get("match_snapshot")
        if not snapshot:
            return
        rt["persist_dirty"] = False
        players = [dict(p) for p in (snapshot.get("players") or [])]
        last_event = (snapshot.get("game") or {}).get("last_event")
        try:
            await save_rlgl_players(mid, players, last_event=last_event)
        except Exception:
            rt["persist_dirty"] = True
            log.exception("Failed to persist RLGL players (mid=%s)", mid)


async def _persist_loop(context, mid):
    rt = runtime.get(mid)
    try:
        while not rt.get("done"):
            await asyncio.sleep(PERSIST_SECONDS)
            if rt.get("done"):
                return
            await _persist_players(mid, rt)
    except asyncio.CancelledError:
        return
    except Exception:
        log.exception("RLGL persist loop crashed (mid=%s)", mid)


# ---------------------------------------------------------------- housekeeping

async def _housekeeping_loop(context, mid):
    """Timer text refresh + trailing-edge group card refresh (never lost to a throttle)."""
    rt = runtime.get(mid)
    last_timer = time.monotonic()
    try:
        while not rt.get("done"):
            await asyncio.sleep(HOUSEKEEPING_SECONDS)
            if rt.get("done"):
                return
            now = time.monotonic()
            if now - last_timer >= TIMER_REFRESH_SECONDS:
                last_timer = now
                _mark_all(rt, OTHER, OTHER)

            since_group = now - rt.get("last_group_refresh", 0.0)
            if (rt.get("group_dirty") or since_group >= GROUP_PERIODIC_SECONDS) \
                    and since_group >= GROUP_THROTTLE_SECONDS:
                rt["group_dirty"] = False
                try:
                    await _refresh_group_round(
                        context, mid, rt.get("light", "green"), force=True
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("RLGL group refresh failed (mid=%s)", mid)
    except asyncio.CancelledError:
        return
    except Exception:
        log.exception("RLGL housekeeping crashed (mid=%s)", mid)


# ---------------------------------------------------------------- light transitions

async def _transition_light(context, mid, light):
    rt = runtime.get(mid)
    async with rt["lock"]:
        snapshot = rt.get("match_snapshot")
        if not snapshot or snapshot.get("stage") != "rlgl" or rt.get("done"):
            return False
        if rt.get("light") == light:
            return True

        rt["phase_seq"] = int(rt.get("phase_seq", 0)) + 1
        token = str(rt["phase_seq"])
        history = rt.setdefault("phase_history", {})
        history[token] = {"light": light, "created_at": time.monotonic()}
        if len(history) > 40:
            for old_token in sorted(history, key=lambda v: int(v))[:-20]:
                history.pop(old_token, None)

        rt["light"] = light
        rt["phase_token"] = token
        rt["state_version"] = int(rt.get("state_version", 0)) + 1
        rt["group_dirty"] = True

        # Racers must see the new signal first; spectators can wait.
        _mark_all(rt, URGENT, OTHER)
    return not rt.get("done")


async def _light_loop(context, mid):
    rt = runtime.get(mid)
    cancelled = False
    try:
        while not rt.get("done"):
            snapshot = rt.get("match_snapshot") or await get_match(mid)
            if not snapshot or snapshot.get("stage") != "rlgl":
                return
            left = _seconds_left(snapshot)
            if left is not None and left <= 0:
                break
            current_light = rt.get("light", "green")
            wait_for = (random.uniform(2.5, 5.0) if current_light == "green"
                        else random.uniform(1.5, 4.0))
            if left is not None:
                wait_for = min(wait_for, max(0.0, float(left)))
            await asyncio.sleep(wait_for)
            if rt.get("done"):
                return
            target = "red" if rt.get("light", "green") == "green" else "green"
            if not await _transition_light(context, mid, target):
                return
    except asyncio.CancelledError:
        cancelled = True
        return
    except Exception as exc:
        log.exception("RLGL signal loop crashed (mid=%s): %s", mid, exc)
    finally:
        if not cancelled and not rt.get("done"):
            await _time_up(context, mid)


# ---------------------------------------------------------------- finish / time up

async def _finish_round(context, mid, survivors=None, *, timed_out=False):
    rt = runtime.get(mid)
    final_light = "green"
    last_event = "The race has ended."

    async with rt["lock"]:
        if rt.get("round_finalizing"):
            return
        snapshot = rt.get("match_snapshot") or await get_match(mid)
        if not snapshot or snapshot.get("stage") != "rlgl":
            return
        rt["round_finalizing"] = True
        rt["done"] = True
        final_light = rt.get("light") or "green"

        grid = rt.get("grid") or {}
        if timed_out:
            for g in grid.values():
                if g["alive"] and not g["reached"]:
                    g["alive"] = False
        _merge_grid_into_match(snapshot, grid)

        players = snapshot.get("players", [])
        final_survivors = [p["id"] for p in players if p.get("alive") and p.get("reached")]
        game_data = snapshot.setdefault("game", {})
        game_data["round_finished"] = True
        game_data["finished_at"] = time.time()
        game_data["last_event"] = game_data.get("last_event") or "The race has ended."
        last_event = game_data["last_event"]
        rt["state_version"] = int(rt.get("state_version", 0)) + 1
        final_version = rt["state_version"]
        final_token = str(rt.get("phase_token", "0"))

    # Persist outside the game lock. Re-read the document first so fields owned by
    # other components (scoreboard / round card ids) are not overwritten with stale values.
    async with rt.setdefault("persist_lock", asyncio.Lock()):
        try:
            fresh = await get_match(mid) or snapshot
            fresh["players"] = snapshot.get("players", [])
            fresh["game"] = snapshot.get("game", {})
            await save_match(fresh)
        except Exception:
            log.exception("Failed to save final RLGL state (mid=%s)", mid)

    await _refresh_all_dms_final(context, mid, snapshot, final_light, final_token, final_version)
    await _refresh_group_round(context, mid, final_light, event=last_event,
                               finished=True, force=True)
    await game_finished(context, mid, final_survivors, "Red Light, Green Light")


async def _time_up(context, mid):
    await _finish_round(context, mid, timed_out=True)


# ---------------------------------------------------------------- phase resolution

def _resolve_button_phase(rt, user_id, supplied_token):
    uid = _uid_key(user_id)
    now = time.monotonic()
    history = rt.get("phase_history", {})
    expected = rt.get("player_phase_tokens", {}).get(uid)
    current_token = str(rt.get("phase_token", "0"))

    if supplied_token is None:
        token = expected or current_token
    else:
        token = str(supplied_token)

    phase = history.get(token)
    if not phase:
        return "stale", None

    if token == current_token:
        return "ok", phase.get("light")

    grace = rt.get("player_grace_tokens", {}).get(uid, {})
    for old_token, expiry in list(grace.items()):
        if expiry <= now:
            grace.pop(old_token, None)
    in_grace = token in grace and grace[token] > now

    if token != expected and not in_grace:
        return "stale", None

    failed_target = rt.get("player_phase_sync_failed", {}).get(uid)
    if failed_target == current_token and token != current_token:
        return "stale", None

    age = now - float(phase.get("created_at", now))
    if age > MAX_STALE_PHASE_AGE_SECONDS and not in_grace:
        return "stale", None

    return "ok", phase.get("light")


# ---------------------------------------------------------------- tap handler

async def on_move(update, context):
    query = update.callback_query
    raw = query.data or ""
    parts = raw.split(":")

    if len(parts) == 4 and parts[0] == "rlgl":
        _, mid, supplied_token, direction = parts
    elif len(parts) == 3 and parts[0] == "rlgl":
        _, mid, direction = parts
        supplied_token = None
    else:
        await safe_answer(query, "Invalid movement control.", True)
        return

    if direction in ("dead", "finished"):
        await safe_answer(query, "No controls available.")
        return
    if direction not in {"up", "left", "right"}:
        await safe_answer(query, "Unknown movement.", True)
        return

    # Look the runtime up without creating one for stale / foreign match ids.
    rt = runtime._RUNTIME.get(mid)
    if rt is None or rt.get("move_queue") is None:
        await safe_answer(query, "This race is over.")
        return
    if rt.get("done") or rt.get("round_finalizing"):
        await safe_answer(query, "The round is finishing.")
        return
    snapshot = rt.get("match_snapshot") or {}
    if not (snapshot.get("game") or {}).get("started_at"):
        await safe_answer(query, "Race hasn't started yet.")
        return

    user_id = query.from_user.id
    uid = str(user_id)

    # Instant, queue-free answers for players who cannot move.
    g = (rt.get("grid") or {}).get(uid)
    if g is None:
        await safe_answer(query, "You are not in this race.", True)
        return
    if not g["alive"]:
        await safe_answer(query, "❌ You are eliminated.")
        return
    if g["reached"]:
        await safe_answer(query, "🏁 You already finished.")
        return

    # Light per-player cooldown so spam-tapping cannot flood the tick.
    now = time.monotonic()
    last_moves = rt.setdefault("last_move_at", {})
    if now - last_moves.get(uid, 0.0) < MOVE_COOLDOWN:
        await safe_answer(query)
        return
    last_moves[uid] = now

    fut = asyncio.get_running_loop().create_future()
    try:
        rt["move_queue"].put_nowait({
            "uid": user_id,
            "direction": direction,
            "token": supplied_token,
            "at": now,
            "fut": fut,
        })
    except asyncio.QueueFull:
        await safe_answer(query, "Too many moves — slow down.")
        return

    try:
        text, alert = await asyncio.wait_for(fut, MOVE_ACK_TIMEOUT)
    except asyncio.TimeoutError:
        text, alert = None, False
    await safe_answer(query, text, alert)