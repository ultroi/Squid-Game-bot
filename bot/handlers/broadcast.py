from __future__ import annotations

import asyncio
import html
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import AsyncIterator

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError
from telegram.ext import ContextTypes

from .. import config
from ..database import broadcast_jobs, groups, users

log = logging.getLogger(__name__)

# Stay comfortably below Telegram's general per-bot messaging throughput.
_SEND_INTERVAL_SECONDS = 0.05  # approximately 20 sends/second, before API latency
_PROGRESS_EVERY = 20
_MAX_RETRY_AFTER_ATTEMPTS = 6
_SESSION_KEY = "broadcast_sessions"
_ACTIVE_JOB_KEY = "active_broadcast_job"
_ACTIVE_JOB_LOCK = asyncio.Lock()
_AUDIENCES = {"users", "groups", "both"}
_AUDIENCE_LABELS = {
    "users": "👤 Users (private messages)",
    "groups": "👥 Active groups",
    "both": "🌍 Users + groups",
}


def _admin_ids() -> set[int]:
    """Read admin IDs from config; support legacy BOT_ADMIN_ID deployments."""
    configured = getattr(config, "BOT_ADMIN_IDS", None)
    if configured:
        return {int(value) for value in configured}
    return {int(getattr(config, "BOT_ADMIN_ID", 6439532660))}


def _is_admin(update: Update) -> bool:
    user = update.effective_user
    return bool(user and user.id in _admin_ids())


def _is_private(update: Update) -> bool:
    chat = update.effective_chat
    return bool(chat and chat.type == "private")


async def _deny(update: Update) -> None:
    message = update.effective_message
    if message:
        await message.reply_text("⛔ This broadcast command is restricted to bot administrators.")


def _sessions(context: ContextTypes.DEFAULT_TYPE) -> dict[int, dict]:
    return context.application.bot_data.setdefault(_SESSION_KEY, {})


def _main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 New Broadcast", callback_data="bc:menu:new")],
        [InlineKeyboardButton("📊 Active Status", callback_data="bc:menu:status"),
         InlineKeyboardButton("📚 History", callback_data="bc:menu:history")],
        [InlineKeyboardButton("✖️ Close", callback_data="bc:cancel")],
    ])


def _audience_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👤 All known users", callback_data="bc:aud:users")],
        [InlineKeyboardButton("👥 Active groups", callback_data="bc:aud:groups")],
        [InlineKeyboardButton("🌍 Users + groups", callback_data="bc:aud:both")],
        [InlineKeyboardButton("✖️ Cancel setup", callback_data="bc:cancel")],
    ])


def _confirmation_keyboard(session: dict) -> InlineKeyboardMarkup:
    notify_label = "🔕 Notifications: OFF" if session.get("disable_notification") else "🔔 Notifications: ON"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 START BROADCAST", callback_data="bc:confirm:send")],
        [InlineKeyboardButton(notify_label, callback_data="bc:confirm:toggle")],
        [InlineKeyboardButton("🎯 Change audience", callback_data="bc:confirm:change"),
         InlineKeyboardButton("✖️ Cancel", callback_data="bc:confirm:cancel")],
    ])


def _progress_keyboard(job: dict) -> InlineKeyboardMarkup | None:
    status = job.get("status")
    if status not in {"running", "paused"}:
        return None
    toggle = "▶️ Resume" if status == "paused" else "⏸ Pause"
    action = "resume" if status == "paused" else "pause"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(toggle, callback_data=f"bcjob:{action}:{job['job_id']}"),
         InlineKeyboardButton("🛑 Cancel", callback_data=f"bcjob:cancel:{job['job_id']}")],
    ])


def _counts_query() -> dict:
    # Telegram IDs only enter users after the bot has seen the user. DM failures
    # are remembered so future user broadcasts do not repeatedly hit blocked DMs.
    return {
        "telegram_id": {"$exists": True},
        "dm_blocked": {"$ne": True},
    }


def _groups_query() -> dict:
    return {"active": True}


async def _audience_counts(audience: str) -> tuple[int, int]:
    user_count = 0
    group_count = 0
    if audience in {"users", "both"}:
        user_count = await users.count_documents(_counts_query())
    if audience in {"groups", "both"}:
        group_count = await groups.count_documents(_groups_query())
    return user_count, group_count


async def _iter_targets(audience: str) -> AsyncIterator[tuple[str, int]]:
    """Yield user and/or group IDs from the database without loading all into RAM."""
    if audience in {"users", "both"}:
        cursor = users.find(_counts_query(), {"telegram_id": 1}).sort("telegram_id", 1)
        async for document in cursor:
            try:
                target_id = int(document["telegram_id"])
            except (KeyError, TypeError, ValueError):
                continue
            yield "user", target_id
    if audience in {"groups", "both"}:
        cursor = groups.find(_groups_query(), {"_id": 1}).sort("_id", 1)
        async for document in cursor:
            try:
                target_id = int(document["_id"])
            except (KeyError, TypeError, ValueError):
                continue
            yield "group", target_id


def _is_sendable_message(message) -> bool:
    """Reject service updates, which Telegram's copyMessage cannot copy."""
    if not message:
        return False
    if message.text is not None or message.caption is not None:
        return True
    media_fields = (
        "animation", "audio", "document", "photo", "sticker", "video", "video_note",
        "voice", "contact", "location", "venue", "poll", "dice", "game", "invoice",
    )
    return any(getattr(message, field, None) is not None for field in media_fields)


def _format_count(value: int) -> str:
    return f"{max(0, int(value)):,}"


def _confirmation_text(session: dict) -> str:
    counts = session.get("target_counts") or {}
    audience = session.get("audience", "users")
    users_count = int(counts.get("users", 0))
    groups_count = int(counts.get("groups", 0))
    total = users_count + groups_count
    lines = [
        "📢 <b>SQUID GAME BROADCAST</b>",
        "━━━━━━━━━━━━━━━━━━",
        f"🎯 <b>Audience:</b> {html.escape(_AUDIENCE_LABELS.get(audience, audience))}",
    ]
    if audience in {"users", "both"}:
        lines.append(f"👤 <b>Known users:</b> {_format_count(users_count)}")
    if audience in {"groups", "both"}:
        lines.append(f"👥 <b>Active groups:</b> {_format_count(groups_count)}")
    lines.extend([
        f"📬 <b>Estimated destinations:</b> {_format_count(total)}",
        f"🔔 <b>Notifications:</b> {'off (silent)' if session.get('disable_notification') else 'on'}",
        "",
        "The message immediately above is the exact content to copy.",
        "Tap <b>START BROADCAST</b> only when you are ready. Delivery can take time for large audiences.",
    ])
    return "\n".join(lines)


async def broadcast_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Entry command: show the interactive broadcast dashboard."""
    if not _is_admin(update):
        await _deny(update)
        return
    if not _is_private(update):
        if update.effective_message:
            await update.effective_message.reply_text("🔒 Please run /broadcast in the bot's private chat.")
        return
        
    await update.effective_message.reply_text(
        "🎛 <b>Broadcast Dashboard</b>\n\nSelect an option below to manage broadcasts:",
        parse_mode="HTML",
        reply_markup=_main_menu_keyboard()
    )


async def handle_content_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Capture the admin's source message after they choose an audience."""
    user = update.effective_user
    message = update.effective_message
    if not user or not message or not _is_private(update) or not _is_admin(update):
        return
    session = _sessions(context).get(user.id)
    if not session or session.get("stage") != "content":
        return
    if not _is_sendable_message(message):
        await message.reply_text("⚠️ This looks like a Telegram service message and cannot be broadcast. Send normal text or supported media.")
        return

    audience = session.get("audience")
    if audience not in _AUDIENCES:
        _sessions(context).pop(user.id, None)
        await message.reply_text("The broadcast session expired. Run /broadcast again.")
        return
    try:
        user_count, group_count = await _audience_counts(audience)
    except Exception:
        log.exception("Could not count broadcast recipients")
        await message.reply_text("⚠️ Could not read the recipient list from MongoDB. No broadcast was started; please try again.")
        return

    session.update({
        "stage": "confirm",
        "source_chat_id": message.chat_id,
        "source_message_id": message.message_id,
        "target_counts": {"users": user_count, "groups": group_count},
        "disable_notification": bool(session.get("disable_notification", False)),
    })
    total = user_count + group_count
    if total <= 0:
        await message.reply_text(
            "⚠️ <b>No recipients found</b> for this audience. Choose a different audience or wait until users/groups are registered.",
            parse_mode="HTML",
            reply_markup=_audience_keyboard(),
        )
        session["stage"] = "audience"
        return
    await message.reply_text(
        _confirmation_text(session),
        parse_mode="HTML",
        reply_markup=_confirmation_keyboard(session),
        disable_web_page_preview=True,
    )


async def on_broadcast_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle audience selection and confirmation buttons."""
    query = update.callback_query
    user = update.effective_user
    if not query or not user:
        return
    if not _is_admin(update):
        await query.answer("⛔ Admin only", show_alert=True)
        return
    
    session = _sessions(context).get(user.id)
    data = query.data or ""

    if data == "bc:cancel":
        _sessions(context).pop(user.id, None)
        await query.answer("Closed")
        try:
            await query.edit_message_text("✖️ Broadcast menu closed.")
        except TelegramError:
            pass
        return

    # Dashboard routing
    if data == "bc:menu:new":
        active = context.application.bot_data.get(_ACTIVE_JOB_KEY)
        warning = "\n\n⚠️ A broadcast is already active. You may prepare a draft, but cannot start a second one." if active else ""
        _sessions(context)[user.id] = {"owner_id": user.id, "stage": "audience"}
        await query.answer()
        await query.edit_message_text(
            "📢 <b>Broadcast Studio</b>\n\n"
            "Choose who should receive your message. You can send text, formatted text, a photo/video with caption, document, audio, sticker, or other copyable Telegram message after choosing.\n\n"
            "<i>Only users known to the bot and groups where the bot is still marked active are included.</i>"
            + warning,
            parse_mode="HTML",
            reply_markup=_audience_keyboard(),
        )
        return
    elif data == "bc:menu:status":
        await query.answer()
        await broadcast_status(update, context)
        return
    elif data == "bc:menu:history":
        await query.answer()
        await broadcast_history(update, context)
        return

    if session is None or int(session.get("owner_id", 0)) != user.id:
        await query.answer("This broadcast session expired. Run /broadcast again.", show_alert=True)
        return

    if data.startswith("bc:aud:"):
        audience = data.split(":", 2)[2]
        if session.get("stage") != "audience" or audience not in _AUDIENCES:
            await query.answer("Choose an audience from the current setup.", show_alert=True)
            return
        session.update({"stage": "content", "audience": audience, "disable_notification": False})
        await query.answer()
        
        # ADDED THE CANCEL BUTTON HERE
        await query.edit_message_text(
            f"✅ Audience: <b>{html.escape(_AUDIENCE_LABELS[audience])}</b>\n\n"
            "Now send the exact text or media message you want to broadcast. It will be copied without a forward header.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✖️ Cancel", callback_data="bc:cancel")]])
        )
        return

    if not data.startswith("bc:confirm:"):
        await query.answer("Unknown broadcast action.", show_alert=True)
        return
    if session.get("stage") != "confirm":
        await query.answer("Send the message content first.", show_alert=True)
        return

    action = data.split(":", 2)[2]
    if action == "cancel":
        _sessions(context).pop(user.id, None)
        await query.answer("Broadcast cancelled")
        try:
            await query.edit_message_text("✖️ Broadcast cancelled. No recipients were messaged.")
        except TelegramError:
            pass
        return
    if action == "change":
        session.clear()
        session.update({"owner_id": user.id, "stage": "audience"})
        await query.answer()
        await query.edit_message_text("🎯 Choose a new audience for this broadcast:", reply_markup=_audience_keyboard())
        return
    if action == "toggle":
        session["disable_notification"] = not bool(session.get("disable_notification"))
        await query.answer("Notifications turned " + ("off" if session["disable_notification"] else "on"))
        try:
            await query.edit_message_text(
                _confirmation_text(session),
                parse_mode="HTML",
                reply_markup=_confirmation_keyboard(session),
                disable_web_page_preview=True,
            )
        except TelegramError:
            log.debug("Could not update broadcast confirmation", exc_info=True)
        return
    if action != "send":
        await query.answer("Unknown action.", show_alert=True)
        return

    await _start_job_from_session(query, context, session, user.id)


async def _start_job_from_session(query, context, session: dict, owner_id: int) -> None:
    application = context.application
    async with _ACTIVE_JOB_LOCK:
        active = application.bot_data.get(_ACTIVE_JOB_KEY)
        if active and active.get("status") in {"running", "paused"}:
            await query.answer("A broadcast is already running. View status in the dashboard.", show_alert=True)
            return
        audience = session.get("audience")
        if audience not in _AUDIENCES or not session.get("source_message_id"):
            await query.answer("Broadcast draft is incomplete. Run /broadcast again.", show_alert=True)
            return
        try:
            user_count, group_count = await _audience_counts(audience)
        except Exception:
            log.exception("Could not recount broadcast recipients before confirmation")
            await query.answer("Cannot reach recipient database; no broadcast started.", show_alert=True)
            return
        total = user_count + group_count
        if total <= 0:
            await query.answer("There are no recipients for this audience.", show_alert=True)
            return

        job_id = uuid.uuid4().hex[:12]
        now = datetime.now(timezone.utc)
        job = {
            "job_id": job_id,
            "owner_id": int(owner_id),
            "audience": audience,
            "source_chat_id": int(session["source_chat_id"]),
            "source_message_id": int(session["source_message_id"]),
            "status_message_chat_id": int(owner_id),
            "status_message_id": int(query.message.message_id),
            "disable_notification": bool(session.get("disable_notification", False)),
            "total": total,
            "processed": 0,
            "sent": 0,
            "failed": 0,
            "blocked": 0,
            "status": "running",
            "started_at": now,
            "finished_at": None,
            "last_error": None,
            "cancel_event": asyncio.Event(),
            "resume_event": asyncio.Event(),
            "task": None,
        }
        job["resume_event"].set()
        try:
            await _persist_job(job, upsert=True)
        except Exception:
            log.exception("Could not create broadcast job record")
            await query.answer("Could not save broadcast job. Nothing was sent.", show_alert=True)
            return

        application.bot_data[_ACTIVE_JOB_KEY] = job
        _sessions(context).pop(owner_id, None)
        await query.answer("Broadcast started")
        try:
            await query.edit_message_text(
                _progress_text(job),
                parse_mode="HTML",
                reply_markup=_progress_keyboard(job),
                disable_web_page_preview=True,
            )
        except TelegramError:
            log.debug("Could not convert confirmation into progress message", exc_info=True)
        job["task"] = application.create_task(_run_broadcast(application, job), name=f"broadcast-{job_id}")


def _persistent_job_fields(job: dict) -> dict:
    fields = (
        "job_id", "owner_id", "audience", "source_chat_id", "source_message_id",
        "disable_notification", "total", "processed", "sent", "failed", "blocked",
        "status", "started_at", "finished_at", "last_error",
    )
    return {key: job.get(key) for key in fields}


async def _persist_job(job: dict, *, upsert: bool = False) -> None:
    await broadcast_jobs.update_one(
        {"_id": job["job_id"]},
        {"$set": _persistent_job_fields(job)},
        upsert=upsert,
    )


def _progress_text(job: dict) -> str:
    status = str(job.get("status", "running")).upper()
    total = max(int(job.get("total", 0)), int(job.get("processed", 0)))
    processed = int(job.get("processed", 0))
    percent = (processed / total * 100) if total else 0.0
    audience = _AUDIENCE_LABELS.get(job.get("audience"), job.get("audience", "unknown"))
    lines = [
        "📢 <b>BROADCAST REPORT</b>",
        "━━━━━━━━━━━━━━━━━━",
        f"🆔 <b>Job:</b> <code>{html.escape(str(job.get('job_id', '—')))}</code>",
        f"📍 <b>Status:</b> {html.escape(status)}",
        f"🎯 <b>Audience:</b> {html.escape(str(audience))}",
        f"📊 <b>Progress:</b> {_format_count(processed)}/{_format_count(total)} ({percent:.1f}%)",
        f"✅ <b>Sent:</b> {_format_count(job.get('sent', 0))}",
        f"⚠️ <b>Failed:</b> {_format_count(job.get('failed', 0))}",
        f"🚫 <b>Blocked/unreachable users:</b> {_format_count(job.get('blocked', 0))}",
    ]
    if job.get("last_error"):
        lines.append(f"📝 <b>Last issue:</b> {html.escape(str(job['last_error'])[:180])}")
    if status in {"RUNNING", "PAUSED"}:
        lines.append("\nUse the buttons below to monitor or control the job.")
    elif status == "CANCELLED":
        lines.append("\n🛑 Cancelled by admin. Remaining recipients were not processed.")
    elif status == "COMPLETED":
        lines.append("\n🏁 Broadcast finished.")
    elif status == "INTERRUPTED":
        lines.append("\n⚠️ Process stopped before delivery completed. It was not auto-resumed to prevent duplicate sends.")
    elif status == "FAILED":
        lines.append("\n❌ Broadcast stopped due to an internal error.")
    return "\n".join(lines)


async def _controlled_sleep(job: dict, seconds: float) -> bool:
    deadline = asyncio.get_running_loop().time() + max(0.0, float(seconds))
    while True:
        if job["cancel_event"].is_set():
            return False
        await job["resume_event"].wait()
        if job["cancel_event"].is_set():
            return False
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return True
        try:
            await asyncio.wait_for(job["cancel_event"].wait(), timeout=min(remaining, 1.0))
            return False
        except asyncio.TimeoutError:
            continue


async def _copy_with_retry(application, job: dict, target_id: int) -> None:
    for attempt in range(_MAX_RETRY_AFTER_ATTEMPTS):
        if job["cancel_event"].is_set():
            raise asyncio.CancelledError
        await job["resume_event"].wait()
        if job["cancel_event"].is_set():
            raise asyncio.CancelledError
        try:
            await application.bot.copy_message(
                chat_id=target_id,
                from_chat_id=job["source_chat_id"],
                message_id=job["source_message_id"],
                disable_notification=bool(job.get("disable_notification", False)),
            )
            return
        except RetryAfter as exc:
            if attempt + 1 >= _MAX_RETRY_AFTER_ATTEMPTS:
                raise
            delay = getattr(exc, "retry_after", 1)
            if hasattr(delay, "total_seconds"):
                delay = delay.total_seconds()
            log.warning("Broadcast %s rate limited; retry %d/%d after %.1fs", job["job_id"], attempt + 1, _MAX_RETRY_AFTER_ATTEMPTS - 1, float(delay))
            if not await _controlled_sleep(job, max(1.0, float(delay)) + 0.15):
                raise asyncio.CancelledError


async def _mark_unreachable(kind: str, target_id: int, error: Exception) -> None:
    message = str(error).lower()
    if kind == "user" and (
        isinstance(error, Forbidden)
        or "chat not found" in message
        or "user is deactivated" in message
        or "bot can't initiate conversation" in message
        or "bot cannot initiate conversation" in message
    ):
        try:
            await users.update_one(
                {"telegram_id": target_id},
                {"$set": {"dm_blocked": True, "dm_blocked_at": datetime.now(timezone.utc)}},
            )
        except Exception:
            log.debug("Could not mark unreachable broadcast user %s", target_id, exc_info=True)
    elif kind == "group" and (
        isinstance(error, Forbidden)
        or "chat not found" in message
        or "bot was kicked" in message
        or "bot is not a member" in message
    ):
        try:
            await groups.update_one({"_id": target_id}, {"$set": {"active": False}})
        except Exception:
            log.debug("Could not deactivate unavailable broadcast group %s", target_id, exc_info=True)


async def _refresh_progress(application, job: dict, *, final: bool = False) -> None:
    try:
        await _persist_job(job)
    except Exception:
        log.exception("Could not persist broadcast progress for %s", job.get("job_id"))
    chat_id = job.get("status_message_chat_id")
    message_id = job.get("status_message_id")
    if not chat_id or not message_id:
        return
    try:
        await application.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=_progress_text(job),
            parse_mode="HTML",
            reply_markup=None if final else _progress_keyboard(job),
            disable_web_page_preview=True,
        )
    except BadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            log.debug("Could not refresh broadcast progress message", exc_info=True)
    except TelegramError:
        log.debug("Could not refresh broadcast progress message", exc_info=True)


async def _run_broadcast(application, job: dict) -> None:
    last_progress = time.monotonic()
    try:
        async for kind, target_id in _iter_targets(job["audience"]):
            if job["cancel_event"].is_set():
                break
            await job["resume_event"].wait()
            if job["cancel_event"].is_set():
                break
            try:
                await _copy_with_retry(application, job, target_id)
                job["sent"] += 1
            except asyncio.CancelledError:
                if job["cancel_event"].is_set():
                    break
                raise
            except Forbidden as exc:
                job["failed"] += 1
                job["blocked"] += 1 if kind == "user" else 0
                job["last_error"] = f"{kind} {target_id}: {str(exc)[:140]}"
                await _mark_unreachable(kind, target_id, exc)
                log.info("Broadcast %s could not reach %s %s (Forbidden)", job["job_id"], kind, target_id)
            except BadRequest as exc:
                job["failed"] += 1
                job["last_error"] = f"{kind} {target_id}: {str(exc)[:140]}"
                await _mark_unreachable(kind, target_id, exc)
                log.info("Broadcast %s rejected for %s %s: %s", job["job_id"], kind, target_id, exc)
            except TelegramError as exc:
                job["failed"] += 1
                job["last_error"] = f"{kind} {target_id}: {str(exc)[:140]}"
                log.warning("Broadcast %s Telegram delivery failed for %s %s: %s", job["job_id"], kind, target_id, exc)
            except Exception as exc:
                job["failed"] += 1
                job["last_error"] = f"{kind} {target_id}: {type(exc).__name__}: {str(exc)[:100]}"
                log.exception("Unexpected error sending broadcast %s to %s %s", job["job_id"], kind, target_id)
            finally:
                if job["cancel_event"].is_set():
                    pass
            
            if job["sent"] + job["failed"] > job["processed"]:
                job["processed"] += 1
            now_mono = time.monotonic()
            if job["processed"] and (job["processed"] % _PROGRESS_EVERY == 0 or now_mono - last_progress >= 3.0):
                await _refresh_progress(application, job)
                last_progress = now_mono
            if job["cancel_event"].is_set():
                break
            if not await _controlled_sleep(job, _SEND_INTERVAL_SECONDS):
                break

        if job["cancel_event"].is_set():
            job["status"] = "cancelled"
        else:
            job["status"] = "completed"
    except asyncio.CancelledError:
        job["status"] = "cancelled" if job["cancel_event"].is_set() else "interrupted"
        if job["status"] == "interrupted":
            job["last_error"] = "Broadcast worker stopped by process shutdown; it was not auto-resumed."
    except Exception as exc:
        job["status"] = "failed"
        job["last_error"] = f"{type(exc).__name__}: {str(exc)[:180]}"
        log.exception("Broadcast worker %s failed unexpectedly", job.get("job_id"))
    finally:
        job["finished_at"] = datetime.now(timezone.utc)
        await _refresh_progress(application, job, final=True)
        if application.bot_data.get(_ACTIVE_JOB_KEY) is job:
            application.bot_data.pop(_ACTIVE_JOB_KEY, None)


async def cancel_setup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update):
        return
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return
    existed = _sessions(context).pop(user.id, None) is not None
    await message.reply_text("✖️ Broadcast setup cancelled." if existed else "No active broadcast setup to cancel.")


async def on_broadcast_job_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user
    if not query or not user:
        return
    if not _is_admin(update):
        await query.answer("⛔ Admin only", show_alert=True)
        return
    parts = (query.data or "").split(":")
    if len(parts) != 3 or parts[0] != "bcjob":
        await query.answer()
        return
    action, job_id = parts[1], parts[2]
    job = context.application.bot_data.get(_ACTIVE_JOB_KEY)
    if not job or job.get("job_id") != job_id:
        await query.answer("This broadcast is no longer active.", show_alert=True)
        return
    if action == "cancel":
        job["cancel_event"].set()
        job["resume_event"].set()
        await query.answer("Cancellation requested")
    elif action == "pause":
        if job.get("status") == "running":
            job["status"] = "paused"
            job["resume_event"].clear()
            await query.answer("Broadcast paused")
        else:
            await query.answer("Broadcast is not running", show_alert=True)
    elif action == "resume":
        if job.get("status") == "paused":
            job["status"] = "running"
            job["resume_event"].set()
            await query.answer("Broadcast resumed")
        else:
            await query.answer("Broadcast is not paused", show_alert=True)
    else:
        await query.answer("Unknown action", show_alert=True)
        return
    await _refresh_progress(context.application, job)


async def broadcast_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return
    active = context.application.bot_data.get(_ACTIVE_JOB_KEY)
    if active:
        await message.reply_text(_progress_text(active), parse_mode="HTML", reply_markup=_progress_keyboard(active))
        return
    try:
        latest = await broadcast_jobs.find_one({"owner_id": user.id}, sort=[("started_at", -1)])
    except Exception:
        log.exception("Could not read broadcast status history")
        await message.reply_text("⚠️ Could not read broadcast history from the database.")
        return
    if not latest:
        await message.reply_text("No active broadcast found. Use the dashboard to create one.")
        return
    await message.reply_text(_progress_text(latest), parse_mode="HTML")


async def broadcast_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return
    try:
        cursor = broadcast_jobs.find({"owner_id": user.id}).sort("started_at", -1).limit(5)
        jobs = await cursor.to_list(length=5)
    except Exception:
        log.exception("Could not read broadcast history")
        await message.reply_text("⚠️ Could not load broadcast history.")
        return
    if not jobs:
        await message.reply_text("No broadcast history yet.")
        return
    lines = ["📚 <b>RECENT BROADCASTS</b>", "━━━━━━━━━━━━━━━━━━"]
    for item in jobs:
        created = item.get("started_at")
        try:
            stamp = created.astimezone(timezone.utc).strftime("%d %b %Y %H:%M UTC")
        except Exception:
            stamp = "unknown time"
        lines.append(
            f"• <code>{html.escape(str(item.get('job_id') or item.get('_id') or '—'))}</code> — "
            f"<b>{html.escape(str(item.get('status', 'unknown')).upper())}</b>\n"
            f"  ✅ {int(item.get('sent', 0) or 0)} sent · ⚠️ {int(item.get('failed', 0) or 0)} failed · {html.escape(stamp)}"
        )
    await message.reply_text("\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)


async def mark_interrupted_jobs() -> None:
    """Mark prior-process jobs interrupted; never auto-resume and risk duplicate sends."""
    try:
        result = await broadcast_jobs.update_many(
            {"status": {"$in": ["running", "paused"]}},
            {"$set": {
                "status": "interrupted",
                "finished_at": datetime.now(timezone.utc),
                "last_error": "Process restarted; not auto-resumed to avoid duplicate delivery.",
            }},
        )
        if result.modified_count:
            log.warning("Marked %d incomplete broadcast job(s) as interrupted", result.modified_count)
    except Exception:
        log.exception("Could not reconcile interrupted broadcast jobs")