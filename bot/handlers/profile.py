"""Player profile card and /mystats command for the Squid Game bot."""
from __future__ import annotations

import asyncio
import html
import io
import logging
import time
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, Update
from telegram.error import BadRequest, TelegramError
from telegram.ext import ContextTypes

from ..repository import get_player_profile, get_player_rank, upsert_user

log = logging.getLogger(__name__)

# ----------------------------------------------------------------------------
# Canvas + Vibrant Cyber-Glass Palette
# ----------------------------------------------------------------------------
_SIZE = (1280, 740)

_BG_TOP = (22, 27, 46)       # Rich midnight indigo
_BG_BOT = (15, 20, 34)       # Deep slate blue
_PANEL = (30, 38, 60)        # Brighter elevated card surface
_PANEL_INNER = (23, 30, 48)  # Inner pill surface
_PANEL_BORDER = (58, 72, 105)
_TRACK = (42, 53, 78)

_WHITE = (248, 250, 252)
_MUTED = (162, 178, 204)
_DIM = (94, 110, 138)

_GREEN = (0, 240, 160)       # Vibrant Player Emerald
_PINK = (255, 46, 126)       # Guard Neon Pink
_RED = (255, 74, 90)         # Elimination Coral Red
_GOLD = (255, 206, 68)       # VIP Championship Gold
_CYAN = (34, 211, 238)       # Electric Cyan

# Stage key -> display label + symbol
_ROUNDS = (
    ("rlgl", "RED LIGHT, GREEN LIGHT", "circle"),
    ("dalgona", "DALGONA CANDY", "triangle"),
    ("marbles", "MARBLES", "square"),
    ("bridge", "GLASS BRIDGE", "circle"),
    ("vote", "THE VOTE", "triangle"),
    ("final", "THE FINAL", "square"),
)

_ROUND_ICONS = {
    "rlgl": "🚦",
    "dalgona": "🍪",
    "marbles": "🎱",
    "bridge": "🌉",
    "vote": "🗳",
    "final": "⚔️",
}

# ----------------- Rate Limiting -----------------
REFRESH_COOLDOWN = 3.0
VIEW_COOLDOWN = 1.0
_LAST_ACTION_TAP: dict[str, float] = {}


def _is_rate_limited(user_id: int, action: str, cooldown: float) -> bool:
    """Check if the user is spamming profile actions."""
    now = time.monotonic()
    key = f"{user_id}:{action}"
    last_tap = _LAST_ACTION_TAP.get(key, 0.0)

    if len(_LAST_ACTION_TAP) > 2000:
        cutoff = now - 60.0
        for k in list(_LAST_ACTION_TAP.keys()):
            if _LAST_ACTION_TAP[k] < cutoff:
                _LAST_ACTION_TAP.pop(k, None)

    if now - last_tap < cooldown:
        return True

    _LAST_ACTION_TAP[key] = now
    return False


# ----------------------------------------------------------------------------
# Drawing helpers
# ----------------------------------------------------------------------------
_BOLD_FONTS = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/segoeuib.ttf",
)
_REGULAR_FONTS = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "C:/Windows/Fonts/segoeui.ttf",
)


@lru_cache(maxsize=128)
def _font(size: int, bold: bool = False):
    for candidate in (_BOLD_FONTS if bold else _REGULAR_FONTS):
        if Path(candidate).exists():
            return ImageFont.truetype(candidate, size=size)
    return ImageFont.load_default()


def _fit_text(draw, text: str, max_width: int, size: int, bold: bool = False):
    font = _font(size, bold)
    while size > 12 and draw.textbbox((0, 0), text, font=font)[2] > max_width:
        size -= 1
        font = _font(size, bold)
    return font


def _center_text(draw, xy, text, font, fill):
    draw.text(xy, text, font=font, fill=fill, anchor="mm")


def _draw_symbol(draw, kind: str, center: tuple[int, int], radius: int, color, width: int = 3):
    x, y = center
    if kind == "circle":
        draw.ellipse((x - radius, y - radius, x + radius, y + radius), outline=color, width=width)
    elif kind == "triangle":
        h = int(radius * 1.05)
        draw.polygon(
            [(x, y - h), (x - radius, y + int(radius * 0.85)), (x + radius, y + int(radius * 0.85))],
            outline=color,
            fill=None,
            width=width,
        )
    else:
        draw.rectangle((x - radius, y - radius, x + radius, y + radius), outline=color, width=width)


def _build_vibrant_background() -> Image.Image:
    """Create a rich, illuminated cyber-arena background with subtle corner glows."""
    w, h = _SIZE
    # Small resolution gradient + bilinear upscale for fast, smooth ambient lighting
    gw, gh = 160, 92
    grad = Image.new("RGB", (gw, gh))
    pixels = grad.load()

    for y in range(gh):
         yf = y / (gh - 1)
         for x in range(gw):
             xf = x / (gw - 1)
             # Base vertical gradient
             r = _BG_TOP[0] * (1 - yf) + _BG_BOT[0] * yf
             g = _BG_TOP[1] * (1 - yf) + _BG_BOT[1] * yf
             b = _BG_TOP[2] * (1 - yf) + _BG_BOT[2] * yf

             # Top-left Pink Ambient Glow
             d_pink = ((xf - 0.08) ** 2 + (yf - 0.12) ** 2) ** 0.5
             glow_p = max(0.0, 1.0 - d_pink * 1.45) ** 2 * 0.32
             r += (_PINK[0] - r) * glow_p
             g += (_PINK[1] - g) * glow_p
             b += (_PINK[2] - b) * glow_p

             # Bottom-right Emerald Green Ambient Glow
             d_green = ((xf - 0.88) ** 2 + (yf - 0.85) ** 2) ** 0.5
             glow_g = max(0.0, 1.0 - d_green * 1.35) ** 2 * 0.26
             r += (_GREEN[0] - r) * glow_g
             g += (_GREEN[1] - g) * glow_g
             b += (_GREEN[2] - b) * glow_g

             pixels[x, y] = (int(min(255, r)), int(min(255, g)), int(min(255, b)))

    bg = grad.resize(_SIZE, Image.Resampling.BILINEAR)
    draw = ImageDraw.Draw(bg)

    # Subtle cyber dot-matrix pattern
    for y in range(28, h, 36):
        for x in range(28, w, 36):
            draw.point((x, y), fill=(48, 60, 90))

    return bg


def _donut(
    image: Image.Image,
    center: tuple[int, int],
    diameter: int,
    parts: list[tuple[float, tuple[int, int, int]]],
    thickness: int,
    track: tuple[int, int, int] | None = _TRACK,
) -> None:
    """Anti-aliased donut chart (supersampled 4x, then pasted)."""
    ss = 4
    d = diameter * ss
    t = thickness * ss
    layer = Image.new("RGBA", (d, d), (0, 0, 0, 0))
    dr = ImageDraw.Draw(layer)
    box = (0, 0, d - 1, d - 1)

    if track is not None:
        dr.arc(box, 0, 360, fill=track, width=t)

    total = sum(max(0.0, v) for v, _ in parts)
    if total > 0:
        nonzero = [p for p in parts if p[0] > 0]
        gap = 3.0 if len(nonzero) > 1 else 0.0
        angle = -90.0
        for value, color in parts:
            if value <= 0:
                continue
            sweep = 360.0 * value / total
            dr.arc(box, angle + gap / 2, angle + sweep - gap / 2, fill=color, width=t)
            angle += sweep

    layer = layer.resize((diameter, diameter), Image.Resampling.LANCZOS)
    r = diameter // 2
    image.paste(layer, (center[0] - r, center[1] - r), layer)


def _make_avatar(raw: bytes | None, display_name: str, size: int) -> Image.Image:
    avatar = None
    if raw:
        try:
            avatar = Image.open(io.BytesIO(raw)).convert("RGB")
            avatar = ImageOps.fit(
                avatar, (size, size), method=Image.Resampling.LANCZOS, centering=(0.5, 0.38)
            )
        except Exception:
            avatar = None
    if avatar is None:
        avatar = Image.new("RGB", (size, size), (34, 46, 72))
        d = ImageDraw.Draw(avatar)
        initials = "".join(part[0] for part in display_name.split()[:2]).upper() or "SG"
        _center_text(d, (size // 2, size // 2), initials[:2], _font(int(size * 0.36), True), _GREEN)

    ss = 4
    mask = Image.new("L", (size * ss, size * ss), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size * ss - 1, size * ss - 1), fill=255)
    mask = mask.resize((size, size), Image.Resampling.LANCZOS)
    avatar = avatar.convert("RGBA")
    avatar.putalpha(mask)
    return avatar


def _player_tier(wins: int, matches: int) -> tuple[str, tuple[int, int, int]]:
    if wins >= 25:
        return "FRONTMAN TIER", _GOLD
    if wins >= 10:
        return "VIP CONTENDER", _PINK
    if wins >= 3:
        return "VETERAN SURVIVOR", _GREEN
    if matches >= 5:
        return "ARENA REGULAR", _CYAN
    return "RECRUIT CONTENDER", _MUTED


def _rate_color(rate: float) -> tuple[int, int, int]:
    if rate >= 70:
        return _GREEN
    if rate >= 40:
        return _GOLD
    return _RED


def _player_number(uid) -> str:
    return f"{(int(uid) % 456) + 1:03d}" if str(uid).isdigit() else "001"


def _as_int(value) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


# ----------------------------------------------------------------------------
# Card renderer (Bright, Clean, Elevated Cyber-Dashboard)
# ----------------------------------------------------------------------------
def _panel(draw, box, accent: tuple[int, int, int] | None = None, radius: int = 24):
    x1, y1, x2, y2 = box
    draw.rounded_rectangle(box, radius=radius, fill=_PANEL, outline=_PANEL_BORDER, width=2)
    if accent:
        # Sleek top neon highlight bar
        draw.rounded_rectangle((x1 + 28, y1, x2 - 28, y1 + 5), radius=2, fill=accent)


def _chart_panel(image, draw, box, title, subtitle, center_value, center_label, accent, parts, legend):
    x1, y1, x2, y2 = box
    _panel(draw, box, accent=accent)

    draw.text((x1 + 28, y1 + 22), title, font=_font(16, True), fill=_WHITE)
    draw.text((x2 - 28, y1 + 24), subtitle, font=_font(11, True), fill=accent, anchor="ra")

    cx, cy = x1 + 112, y1 + 158
    _donut(image, (cx, cy), 148, parts, thickness=20)
    _center_text(draw, (cx, cy - 7), center_value, _font(30, True), _WHITE)
    _center_text(draw, (cx, cy + 20), center_label, _font(11, True), _MUTED)

    lx = x1 + 212
    for i, (label, value, color) in enumerate(legend):
        ly = y1 + 88 + i * 74
        draw.rounded_rectangle((lx, ly, x2 - 24, ly + 60), radius=14, fill=_PANEL_INNER, outline=(45, 58, 86), width=1)
        draw.rounded_rectangle((lx + 10, ly + 14, lx + 16, ly + 46), radius=3, fill=color)
        draw.text((lx + 28, ly + 11), label, font=_font(11, True), fill=_MUTED)
        draw.text((lx + 28, ly + 28), value, font=_fit_text(draw, value, x2 - lx - 60, 22, True), fill=_WHITE)


def render_profile_card(profile: dict, rank: int | None, avatar_bytes: bytes | None) -> io.BytesIO:
    width, height = _SIZE
    image = _build_vibrant_background()
    draw = ImageDraw.Draw(image)

    uid = profile.get("telegram_id", 0)
    player_num = _player_number(uid)
    name = str(profile.get("first_name") or "Player").strip() or "Player"
    handle = f"@{profile['username']}" if profile.get("username") else "No public handle"
    tag = str(profile.get("selected_tag") or "UNRANKED SURVIVOR").upper()

    wins = _as_int(profile.get("wins"))
    matches_played = _as_int(profile.get("matches_played"))
    matches_completed = _as_int(profile.get("matches_completed"))
    match_losses = _as_int(profile.get("matches_lost"))
    rounds_won = _as_int(profile.get("rounds_survived"))
    rounds_lost = _as_int(profile.get("rounds_eliminated"))
    rounds_total = max(_as_int(profile.get("rounds_played")), rounds_won + rounds_lost)

    has_match_outcomes = "matches_lost" in profile or "matches_completed" in profile
    outcome_total = wins + match_losses
    if matches_completed:
        outcome_total = max(outcome_total, matches_completed)
    win_rate = (wins / outcome_total * 100) if has_match_outcomes and outcome_total else None
    survival_rate = (rounds_won / rounds_total * 100) if rounds_total else 0.0
    tier_label, tier_color = _player_tier(wins, matches_played)

    round_stats = profile.get("round_stats") or profile.get("stage_stats") or {}

    # ---- Top Header Bar ----------------------------------------------------
    _draw_symbol(draw, "circle", (52, 42), 11, _PINK, width=3)
    _draw_symbol(draw, "triangle", (82, 42), 11, _WHITE, width=3)
    _draw_symbol(draw, "square", (112, 42), 11, _GREEN, width=3)
    draw.text((138, 42), "SQUID GAME  ·  OFFICIAL DOSSIER", font=_font(20, True), fill=_WHITE, anchor="lm")

    # Top-right Player Number Pill
    draw.rounded_rectangle((width - 215, 24, width - 38, 60), radius=12, fill=(20, 48, 46), outline=_GREEN, width=2)
    _center_text(draw, (width - 126, 42), f"CONTENDER #{player_num}", _font(14, True), _GREEN)

    # ---- Left Panel: Player Identity ---------------------------------------
    lx1, ly1, lx2, ly2 = 38, 80, 418, 702
    _panel(draw, (lx1, ly1, lx2, ly2), accent=_PINK, radius=26)
    cx = (lx1 + lx2) // 2

    # Glowing Avatar Ring
    avatar_size = 152
    cy = ly1 + 122
    _donut(image, (cx, cy), avatar_size + 24, [(1, (38, 85, 80))], thickness=4, track=None)
    _donut(image, (cx, cy), avatar_size + 12, [(1, _GREEN)], thickness=4, track=None)
    avatar = _make_avatar(avatar_bytes, name, avatar_size)
    image.paste(avatar, (cx - avatar_size // 2, cy - avatar_size // 2), avatar)

    # Floating Player Number Tag over bottom of avatar
    draw.rounded_rectangle((cx - 56, cy + 58, cx + 56, cy + 88), radius=10, fill=_PINK, outline=_PANEL, width=3)
    _center_text(draw, (cx, cy + 73), f"NO. {player_num}", _font(14, True), _WHITE)

    # Name & Handle
    _center_text(draw, (cx, ly1 + 244), name, _fit_text(draw, name, 330, 30, True), _WHITE)
    _center_text(draw, (cx, ly1 + 278), handle[:30], _font(15), _MUTED)

    # 2-Column Stat Pills inside Identity Card (Rank & Played)
    rank_label = f"#{rank:,}" if rank is not None else "UNRANKED"
    rank_color = _GOLD if rank is not None else _MUTED

    p_w = (lx2 - lx1 - 56 - 14) // 2
    b1_x1, b1_y1 = lx1 + 28, ly1 + 312
    b2_x1 = b1_x1 + p_w + 14

    # Pill 1: Global Rank
    draw.rounded_rectangle((b1_x1, b1_y1, b1_x1 + p_w, b1_y1 + 78), radius=16, fill=_PANEL_INNER, outline=(50, 64, 95), width=1)
    draw.text((b1_x1 + 16, b1_y1 + 16), "GLOBAL RANK", font=_font(11, True), fill=_MUTED)
    draw.text((b1_x1 + 16, b1_y1 + 38), rank_label, font=_fit_text(draw, rank_label, p_w - 28, 22, True), fill=rank_color)

    # Pill 2: Matches Played
    draw.rounded_rectangle((b2_x1, b1_y1, b2_x1 + p_w, b1_y1 + 78), radius=16, fill=_PANEL_INNER, outline=(50, 64, 95), width=1)
    draw.text((b2_x1 + 16, b1_y1 + 16), "GAMES PLAYED", font=_font(11, True), fill=_MUTED)
    draw.text((b2_x1 + 16, b1_y1 + 38), f"{matches_played:,}", font=_fit_text(draw, f"{matches_played:,}", p_w - 28, 22, True), fill=_CYAN)

    # Full-width Pill: Clearance Tier
    c_y1 = b1_y1 + 94
    draw.rounded_rectangle((lx1 + 28, c_y1, lx2 - 28, c_y1 + 76), radius=16, fill=_PANEL_INNER, outline=(50, 64, 95), width=1)
    draw.rounded_rectangle((lx1 + 40, c_y1 + 16, lx1 + 46, c_y1 + 60), radius=3, fill=tier_color)
    draw.text((lx1 + 60, c_y1 + 16), "CLEARANCE TIER", font=_font(11, True), fill=_MUTED)
    draw.text((lx1 + 60, c_y1 + 36), tier_label, font=_fit_text(draw, tier_label, 270, 20, True), fill=tier_color)

    # Full-width Pill: Equipped Title
    t_y1 = c_y1 + 92
    draw.rounded_rectangle((lx1 + 28, t_y1, lx2 - 28, t_y1 + 78), radius=16, fill=(38, 24, 48), outline=(110, 42, 80), width=1)
    draw.text((lx1 + 46, t_y1 + 16), "EQUIPPED TITLE", font=_font(11, True), fill=_PINK)
    draw.text((lx1 + 46, t_y1 + 38), tag[:26], font=_fit_text(draw, tag[:26], 290, 19, True), fill=_WHITE)

    # ---- Right Top: Donut Chart Panels -------------------------------------
    rx1, rx2 = 442, 1242
    pw = (rx2 - rx1 - 22) // 2

    _chart_panel(
        image, draw, (rx1, 80, rx1 + pw, 352),
        "MATCH RECORD", "CHAMPIONSHIP",
        f"{win_rate:.0f}%" if win_rate is not None else "—", "WIN RATE", _GOLD,
        [(wins, _GOLD), (match_losses, _RED)] if has_match_outcomes and outcome_total else [],
        [
            ("VICTORIES", f"{wins:,}", _GOLD),
            ("ELIMINATED", f"{match_losses:,}" if has_match_outcomes else "—", _RED),
        ],
    )
    _chart_panel(
        image, draw, (rx1 + pw + 22, 80, rx2, 352),
        "STAGE SURVIVAL", f"{rounds_total:,} TOTAL ROUNDS",
        f"{survival_rate:.0f}%", "SURVIVAL", _GREEN,
        [(rounds_won, _GREEN), (rounds_lost, _RED)] if rounds_total else [],
        [
            ("CLEARED", f"{rounds_won:,}", _GREEN),
            ("ELIMINATED", f"{rounds_lost:,}", _RED),
        ],
    )

    # ---- Right Bottom: Round Performance Breakdown -------------------------
    py1, py2 = 374, 702
    _panel(draw, (rx1, py1, rx2, py2), accent=_CYAN, radius=24)
    draw.text((rx1 + 28, py1 + 22), "ROUND-BY-ROUND TELEMETRY", font=_font(16, True), fill=_WHITE)
    draw.text((rx2 - 28, py1 + 24), "CLEARED / PLAYED  ·  SUCCESS RATE", font=_font(11, True), fill=_MUTED, anchor="ra")

    bar_x1, bar_x2 = rx1 + 315, rx2 - 195
    for idx, (key, label, sym) in enumerate(_ROUNDS):
        cy_row = py1 + 74 + idx * 43
        stats = round_stats.get(key) or {}
        s_ok = _as_int(stats.get("wins") if "wins" in stats else stats.get("survived"))
        s_out = _as_int(stats.get("losses") if "losses" in stats else stats.get("eliminated"))
        s_total = max(_as_int(stats.get("played")), s_ok + s_out)
        rate = (s_ok / s_total * 100) if s_total else 0.0

        # Subtle row highlight pill
        draw.rounded_rectangle((rx1 + 20, cy_row - 16, rx2 - 20, cy_row + 16), radius=10, fill=_PANEL_INNER)

        # Round geometric icon + title
        sym_color = _rate_color(rate) if s_total else _DIM
        _draw_symbol(draw, sym, (rx1 + 42, cy_row), 7, sym_color, width=2)
        draw.text(
            (rx1 + 62, cy_row),
            label.title(),
            font=_font(14, True),
            fill=_WHITE if s_total else _MUTED,
            anchor="lm",
        )

        # Progress bar track
        draw.rounded_rectangle((bar_x1, cy_row - 6, bar_x2, cy_row + 6), radius=6, fill=_TRACK)
        if s_total:
            if s_out:
                draw.rounded_rectangle((bar_x1, cy_row - 6, bar_x2, cy_row + 6), radius=6, fill=(110, 42, 58))
            if s_ok:
                fill_x = bar_x1 + max(12, int((bar_x2 - bar_x1) * s_ok / s_total))
                draw.rounded_rectangle((bar_x1, cy_row - 6, fill_x, cy_row + 6), radius=6, fill=_rate_color(rate))

            # Score counter + Percentage on the right
            draw.text((rx2 - 110, cy_row), f"{s_ok}/{s_total}", font=_font(13, True), fill=_MUTED, anchor="rm")
            draw.text((rx2 - 34, cy_row), f"{rate:.0f}%", font=_font(16, True), fill=_rate_color(rate), anchor="rm")
        else:
            draw.text((rx2 - 110, cy_row), "0/0", font=_font(13, True), fill=_DIM, anchor="rm")
            draw.text((rx2 - 34, cy_row), "—", font=_font(16, True), fill=_DIM, anchor="rm")

    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    output.seek(0)
    output.name = "squid_game_dossier.png"
    return output


# ----------------------------------------------------------------------------
# Telegram handlers
# ----------------------------------------------------------------------------
async def _get_avatar_bytes(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bytes | None:
    try:
        photos = await context.bot.get_user_profile_photos(user_id=user_id, limit=1)
        if not photos.photos:
            return None
        photo = photos.photos[0][-1]
        file = await context.bot.get_file(photo.file_id)
        return bytes(await file.download_as_bytearray())
    except (TelegramError, OSError, ValueError):
        log.debug("Unable to fetch Telegram avatar for user %s", user_id, exc_info=True)
        return None
    except Exception:
        log.debug("Unexpected avatar fetch failure for user %s", user_id, exc_info=True)
        return None


def _profile_caption(profile: dict, rank: int | None) -> str:
    uid = profile.get("telegram_id", 0)
    player_num = _player_number(uid)
    name = html.escape(str(profile.get("first_name") or "Player"))
    wins = _as_int(profile.get("wins"))
    matches_played = _as_int(profile.get("matches_played"))
    matches_completed = _as_int(profile.get("matches_completed"))
    match_losses = _as_int(profile.get("matches_lost"))
    has_match_outcomes = "matches_lost" in profile or "matches_completed" in profile
    outcome_total = wins + match_losses
    if matches_completed:
        outcome_total = max(outcome_total, matches_completed)
    win_rate = (wins / outcome_total * 100) if has_match_outcomes and outcome_total else None
    tier_label, _ = _player_tier(wins, matches_played)
    win_rate_text = f"{win_rate:.1f}%" if win_rate is not None else "Not enough history"

    rank_text = f"#{rank:,}" if rank is not None else "Unranked"
    return (
        f"🔺 <b>SQUID GAME · PLAYER #{player_num}</b>\n"
        f"👤 <b>Contender:</b> <a href=\"tg://user?id={uid}\">{name}</a>\n"
        f"🎖 <b>Clearance:</b> <code>{tier_label}</code>  ·  🌍 <b>Rank:</b> <code>{rank_text}</code>\n"
        f"🏆 <b>Wins:</b> <code>{wins}</code>  ·  🎮 <b>Played:</b> <code>{matches_played}</code>  ·  "
        f"✅ <b>Completed:</b> <code>{matches_completed}</code>  ·  💀 <b>Losses:</b> <code>{match_losses if has_match_outcomes else '—'}</code>\n"
        f"📈 <b>Win rate:</b> <code>{win_rate_text}</code>\n\n"
        "<i>Use the button below to refresh your live survival telemetry.</i>"
    )


def _profile_keyboard(user_id: int, view: str = "overview") -> InlineKeyboardMarkup:
    overview_label = "✅ Overview" if view == "overview" else "📊 Overview"
    rounds_label = "✅ Round stats" if view == "rounds" else "🎮 Round stats"
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(overview_label, callback_data=f"prof:overview:{user_id}"),
                InlineKeyboardButton(rounds_label, callback_data=f"prof:rounds:{user_id}"),
            ],
            [InlineKeyboardButton("🔄 Refresh dossier", callback_data=f"prof:refresh:{user_id}")],
        ]
    )


def _rounds_caption(profile: dict, rank: int | None) -> str:
    name = html.escape(str(profile.get("first_name") or "Player"))
    uid = profile.get("telegram_id", 0)
    stats_by_round = profile.get("round_stats") or profile.get("stage_stats") or {}
    rank_text = f"#{rank:,}" if rank is not None else "Unranked"

    lines = [
        "🎮 <b>SQUID GAME · ROUND PERFORMANCE</b>",
        f'👤 <b>Player:</b> <a href="tg://user?id={uid}">{name}</a> · 🌍 Rank <code>{rank_text}</code>',
        "━━━━━━━━━━━━━━━━━━━━",
    ]

    total_cleared = 0
    total_attempts = 0

    for key, label, _symbol in _ROUNDS:
        stats = stats_by_round.get(key) or {}
        won = _as_int(stats.get("wins") if "wins" in stats else stats.get("survived"))
        lost = _as_int(stats.get("losses") if "losses" in stats else stats.get("eliminated"))
        played = max(_as_int(stats.get("played")), won + lost)

        total_cleared += won
        total_attempts += played

        icon = _ROUND_ICONS.get(key, "🎮")

        if played > 0:
            rate = f"{won / played * 100:.0f}%"
            lines.append(
                f"\n{icon} <b>{label}</b>\n"
                f"   └ ✅ Cleared: <b>{won}</b>/{played} · 💀 Out: <b>{lost}</b> · 📈 <code>{rate}</code>"
            )
        else:
            lines.append(
                f"\n{icon} <b>{label}</b>\n"
                f"   └ <i>No attempts recorded yet</i>"
            )

    lines.extend([
        "\n━━━━━━━━━━━━━━━━━━━━",
        f"📊 <b>Total Rounds Cleared:</b> <code>{total_cleared} / {total_attempts}</code>",
        "<i>Tap buttons below to toggle overview or refresh live stats.</i>",
    ])

    caption = "\n".join(lines)
    if len(caption) > 1020:
        caption = caption[:1010].rsplit("\n", 1)[0] + "…"
    return caption


async def _handle_profile_view(update: Update, context: ContextTypes.DEFAULT_TYPE, view: str) -> None:
    query = update.callback_query
    if query is None or query.from_user is None:
        return

    parts = (query.data or "").split(":")
    if len(parts) != 3 or parts[0] != "prof" or parts[1] != view:
        return

    target_uid = int(parts[2]) if parts[2].isdigit() else query.from_user.id
    if query.from_user.id != target_uid:
        await query.answer("🔒 This dossier belongs to another player. Use /mystats for your own.", show_alert=True)
        return

    if _is_rate_limited(query.from_user.id, "view", VIEW_COOLDOWN):
        await query.answer("⏳ Please slow down!", show_alert=False)
        return

    try:
        await upsert_user(query.from_user)
        profile = await get_player_profile(target_uid)
        if not profile:
            await query.answer("⚠️ Profile not found.", show_alert=True)
            return
        rank = await get_player_rank(target_uid, _as_int(profile.get("wins")))
        caption = _rounds_caption(profile, rank) if view == "rounds" else _profile_caption(profile, rank)
        await query.edit_message_caption(
            caption=caption,
            parse_mode="HTML",
            reply_markup=_profile_keyboard(target_uid, view=view),
        )
        await query.answer()
    except BadRequest as exc:
        if "message is not modified" in str(exc).lower():
            await query.answer("✅ Already showing this view.")
            return
        log.warning("Could not switch profile view for user %s: %s", target_uid, exc)
        await query.answer("⚠️ Could not open this view right now.", show_alert=True)
    except Exception:
        log.exception("Failed to display %s stats for user %s", view, target_uid)
        await query.answer("⚠️ Could not open this view right now.", show_alert=True)


async def on_profile_round_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _handle_profile_view(update, context, "rounds")


async def on_profile_overview(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _handle_profile_view(update, context, "overview")


async def _build_card(context, user_id: int, profile: dict) -> tuple[io.BytesIO, int | None]:
    rank = await get_player_rank(user_id, _as_int(profile.get("wins")))
    avatar_bytes = await _get_avatar_bytes(context, user_id)
    card = await asyncio.to_thread(render_profile_card, profile, rank, avatar_bytes)
    return card, rank


async def mystats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return

    try:
        await upsert_user(user)
        profile = await get_player_profile(user.id)
        if profile is None:
            await message.reply_text("Your player profile could not be loaded. Please try again.")
            return

        card, rank = await _build_card(context, user.id, profile)
        await message.reply_photo(
            photo=card,
            caption=_profile_caption(profile, rank),
            parse_mode="HTML",
            reply_markup=_profile_keyboard(user.id),
        )
    except Exception:
        log.exception("Failed to build /mystats profile for user %s", user.id)
        await message.reply_text("⚠️ Couldn't build your profile right now. Please try again in a moment.")


async def on_profile_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Callback handler for `prof:refresh:<uid>` button presses with flood protection."""
    query = update.callback_query
    if query is None or query.from_user is None:
        return

    parts = (query.data or "").split(":")
    if len(parts) != 3 or parts[0] != "prof":
        return

    target_uid = int(parts[2]) if parts[2].isdigit() else query.from_user.id
    if query.from_user.id != target_uid:
        await query.answer("🔒 This dossier belongs to another player. Use /mystats for your own.", show_alert=True)
        return

    if _is_rate_limited(query.from_user.id, "refresh", REFRESH_COOLDOWN):
        await query.answer("⏳ Refreshing too fast! Please wait a moment.", show_alert=False)
        return

    try:
        await upsert_user(query.from_user)
        profile = await get_player_profile(target_uid)
        if not profile:
            await query.answer("⚠️ Profile not found.", show_alert=True)
            return

        card, rank = await _build_card(context, target_uid, profile)
        await query.edit_message_media(
            media=InputMediaPhoto(
                media=card,
                caption=_profile_caption(profile, rank),
                parse_mode="HTML",
            ),
            reply_markup=_profile_keyboard(target_uid),
        )
        await query.answer("✅ Dossier updated!")
    except BadRequest as exc:
        if "not modified" in str(exc).lower():
            await query.answer("✅ Dossier is already up to date!")
            return
        await query.answer("⚠️ Could not refresh right now.", show_alert=True)
    except Exception:
        log.exception("Failed to refresh dossier for user %s", target_uid)
        await query.answer("⚠️ Could not refresh right now.", show_alert=True)