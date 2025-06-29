# bot/client_tasks/task_utils.py
import asyncio
import logging
import random
from typing import TYPE_CHECKING, Optional
from pyrogram.enums import UserStatus

from bot.utils.gemini import get_unique_text_gemini

if TYPE_CHECKING:
    from aiogram import Bot

logger = logging.getLogger(__name__)


async def get_unique_text_with_fallback(
    original_text: str,
    user_id: int,
    ai_settings: dict,
    stats_lock: asyncio.Lock,
    stats_dict: dict,
    log_prefix: str
) -> str:
    """
    Tries to get a unique version of the text using AI.
    If it fails, it logs the error, updates stats, and returns the original text.
    """
    unique_text, error_msg = await get_unique_text_gemini(
        original_text, ai_settings["api_key"], ai_settings.get("prompt")
    )

    if unique_text:
        return unique_text

    # AI failed, log the error and update stats
    log_msg = "Не удалось уникализировать текст."
    if error_msg:
        log_msg += f" Причина: {error_msg}"
    logger.warning(f"{log_prefix}: {log_msg}")

    async with stats_lock:
        if user_id in stats_dict:
            stats = stats_dict[user_id]
            stats["errors"] += 1
            detail = error_msg or "Неизвестная ошибка уникализации"
            stats["error_details"].append(detail)

    return original_text  # Fallback to original


async def record_worker_session_failure(
    user_id: int,
    phone_for_log: str,
    reason: str,
    stats_lock: asyncio.Lock,
    stats_dict: dict,
    log_prefix: str,
    bot: "Optional[Bot]" = None,
    notify_user: bool = False,
    notification_text: str = ""
):
    """Safely records a session failure in the statistics dictionary."""
    async with stats_lock:
        if user_id not in stats_dict:
            logger.warning(f"{log_prefix}: Stats for {user_id} not found. Cannot record failure.")
            return

        stats = stats_dict[user_id]
        # Check if this failure for this phone has already been recorded
        if not any(d.get('phone') == phone_for_log for d in stats.get("failed_sessions", [])):
            stats.get("failed_sessions", []).append({"phone": phone_for_log, "reason": reason})
            stats["errors"] += 1
            if notify_user and notification_text and bot:
                try:
                    # Notifications from spam_loop use HTML
                    await bot.send_message(chat_id=user_id, text=notification_text, parse_mode="HTML")
                except Exception as e_notify:
                    logger.error(f"{log_prefix}: Could not notify {user_id} about failure: {e_notify}")


def is_user_active(status: Optional[UserStatus], filter_level: str) -> bool:
    """Checks if a user's status matches the desired activity filter level."""
    if filter_level == 'all' or status is None:
        return True

    recent_statuses = [UserStatus.ONLINE, UserStatus.OFFLINE, UserStatus.RECENTLY]
    if filter_level == 'recent':
        return status in recent_statuses

    if filter_level == 'week':
        return status in recent_statuses or status == UserStatus.LAST_WEEK

    return True  # Default to no filter if an unknown value is provided