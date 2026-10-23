"""Button-driven overall and per-round Squid Game leaderboards."""
from __future__ import annotations

import html
import logging
import time
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from ..repository import get_leaderboard, get_player_profile, get_player_rank_for_scope

log = logging.getLogger(__name__)

MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}
SCOPES = {
    "overall": ("🏆 OVERALL CHAMPIONS", "Full-match victories", "wins"),
    "rlgl": ("🟢 RED LIGHT, GREEN LIGHT", "Round wins · qualified", "round"),
    "dalgona": ("🍪 DALGONA CANDY", "Round wins · qualified", "round"),
    "marbles": ("🎱 MARBLES", "Round wins · advanced", "round"),
    "bridge": ("🌉 GLASS BRIDGE", "Round wins · crossed safely", "round"),
    "vote": ("🗳 VOTING ROUND", "Round wins · survived vote", "round"),
    "final": ("⚔️ FINAL BRACKET", "Round wins · advanced", "round"),
}

# ----------------- Rate Limiting -----------------
BUTTON_COOLDOWN = 1.5
_LAST_BUTTON_TAP: dict[int, float] = {}


def _is_rate_limited(user_id: int) -> bool:
    """Check if the user is clicking buttons too fast."""
    now = time.monotonic()
    last_tap = _LAST_BUTTON_TAP.get(user_id, 0.0)

    if len(_LAST_BUTTON_TAP) > 2000:
        cutoff = now - 60.0
        for uid in list(_LAST_BUTTON_TAP.keys()):
            if _LAST_BUTTON_TAP[uid] < cutoff:
                _LAST_BUTTON_TAP.pop(uid, None)

    if now - last_tap < BUTTON_COOLDOWN:
        return True

    _LAST_BUTTON_TAP[user_id] = now
    return False


def _keyboard(active_scope: str, owner_id: int) -> InlineKeyboardMarkup:
    labels = {
        "overall": "🏆 Overall",
        "rlgl": "🟢 RLGL",
        "dalgona": "🍪 Dalgona",
        "marbles": "🎱 Marbles",
        "bridge": "🌉 Bridge",
        "vote": "🗳 Voting",
        "final": "⚔️ Final",
    }

    def button(scope: str) -> InlineKeyboardButton:
        label = labels[scope]
        if scope == active_scope:
            label = f"✅ {label}"
        # Callback data me scope ke sath owner_id bhi store hogi
        return InlineKeyboardButton(label, callback_data=f"lb:{scope}:{owner_id}")

    return InlineKeyboardMarkup([
        [button("overall"), button("rlgl"), button("dalgona")],
        [button("marbles"), button("bridge"), button("vote")],
        [button("final")],
    ])


async def _render_leaderboard(scope: str, viewer_id: int | None) -> str:
    if scope not in SCOPES:
        scope = "overall"
    title, score_description, mode = SCOPES[scope]
    players = await get_leaderboard(limit=10, scope=scope)
    lines = [
        "🔺 <b>SQUID GAME · GLOBAL RANKINGS</b> 🔺",
        f"<b>{title}</b>",
        "━━━━━━━━━━━━━━━━━━━━",
    ]

    if not players:
        if scope == "overall":
            lines.append("No match champions yet. Be the first to claim a victory!")
        else:
            lines.append("No recorded results for this round yet. Play a match to start the rankings!")
    else:
        for position, player in enumerate(players, start=1):
            uid = int(player.get("telegram_id", 0) or 0)
            raw_name = player.get("first_name") or f"Player {uid}"
            name = html.escape(str(raw_name))
            linked_name = f'<a href="tg://user?id={uid}">{name}</a>' if uid else name
            medal = MEDALS.get(position, f"<b>{position:02d}.</b>")

            if mode == "wins":
                score = int(player.get("wins", 0) or 0)
                details = f"🏆 <b>{score}</b> {'win' if score == 1 else 'wins'}"
            else:
                stats = (player.get("round_stats") or {}).get(scope) or {}
                score = int(stats.get("wins", 0) or 0)
                details = f"✅ <b>{score}</b> {'win' if score == 1 else 'wins'}"

            lines.append(f"{medal} {linked_name}\n   └ {details}")

    lines.extend([
        "━━━━━━━━━━━━━━━━━━━━",
        f"<i>Ranking metric: {html.escape(score_description)}</i>",
    ])

    if viewer_id is not None:
        rank = await get_player_rank_for_scope(viewer_id, scope)
        if rank is None:
            lines.append("\n👤 <b>Your standing:</b>\n   └ No recorded result in this category yet.")
        else:
            lines.append(f"\n👤 <b>Your rank:</b> <b>#{rank}</b>")
    return "\n".join(lines)


async def leaderboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show global championship rankings with owner-restricted buttons."""
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return
    try:
        text = await _render_leaderboard("overall", user.id)
        await message.reply_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=_keyboard("overall", user.id),
            disable_web_page_preview=True,
        )
    except Exception:
        log.exception("Failed to generate leaderboard")
        await message.reply_text("⚠️ The leaderboard is temporarily unavailable. Please try again shortly.")


async def on_leaderboard_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle leaderboard button callbacks with rate-limiting and user-authorization checks."""
    query = update.callback_query
    if query is None or query.message is None or query.from_user is None:
        return

    parts = (query.data or "").split(":")
    # Expected format: lb:<scope>:<owner_id>
    if len(parts) != 3 or parts[0] != "lb" or parts[1] not in SCOPES:
        await query.answer()
        return

    scope = parts[1]
    try:
        owner_id = int(parts[2])
    except ValueError:
        await query.answer()
        return

    # Check: Kya wahi user click kar raha hai jisne command run ki thi?
    if query.from_user.id != owner_id:
        await query.answer("❌ Not allowed.", show_alert=True)
        return

    # Check button cooldown
    if _is_rate_limited(query.from_user.id):
        await query.answer("⏳ Please slow down! Don't spam.", show_alert=False)
        return

    try:
        text = await _render_leaderboard(scope, owner_id)
        await query.edit_message_text(
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=_keyboard(scope, owner_id),
            disable_web_page_preview=True,
        )
        await query.answer()
    except BadRequest as exc:
        if "message is not modified" in str(exc).lower():
            await query.answer("Already showing this category.")
            return
        log.exception("Unable to switch leaderboard to %s", scope)
        await query.answer("Could not load this leaderboard.", show_alert=True)
    except Exception:
        log.exception("Unable to switch leaderboard to %s", scope)
        await query.answer("Could not load this leaderboard.", show_alert=True)