# bot/utils/safe_task.py
import asyncio
import html
import logging
from typing import Coroutine

from aiogram import Bot

logger = logging.getLogger(__name__)

def create_safe_task(coro: Coroutine, user_id: int, bot: Bot, task_name: str):
    """
    Creates a background task that is safely wrapped to handle exceptions,
    log them, and notify the user.
    """
    async def safe_wrapper():
        try:
            await coro
        except asyncio.CancelledError:
            logger.info(f"Safe task '{task_name}' for user {user_id} was cancelled.")
        except Exception as e:
            logger.critical(f"Unhandled exception in safe task '{task_name}' for user {user_id}: {e}", exc_info=True)
            try:
                await bot.send_message(
                    user_id,
                    f"⚠️ Произошла критическая ошибка в фоновой задаче '<b>{html.escape(task_name)}</b>'.\n"
                    f"Задача была аварийно остановлена. Подробности в логах.\n"
                    f"Ошибка: <code>{html.escape(type(e).__name__)}</code>"
                )
            except Exception as notify_exc:
                logger.error(f"Failed to notify user {user_id} about task failure: {notify_exc}")

    return asyncio.create_task(safe_wrapper())