# bot/client_tasks/scheduled_tasks.py
import asyncio
import json
import logging

from aiogram import Bot
from bot.keyboards import tasks_keyboard

logger = logging.getLogger(__name__)

async def run_scheduled_spam(bot: Bot, user_id: int, job_id: str, task_params_json: str):
    """
    This function is executed by the scheduler.
    It checks if a spam task is already running for the user and, if not, starts one.
    """
    # --- ИЗМЕНЕНО: Локальный импорт для разрыва циклической зависимости ---
    from bot.client_tasks.client_manager import (
        ACTIVE_SPAM_TASKS, ATTACK_STATUS, SPAM_STATUS, SPAM_STATUS_LOCK, STOP_EVENTS
    )
    # --- ИЗМЕНЕНО: Локальный импорт для разрыва циклической зависимости ---
    from bot.client_tasks.spam_loop import spam_loop_task

    log_prefix = f"SCHEDULER_JOB [{job_id}]"
    logger.info(f"{log_prefix}: Triggered for user {user_id}.")

    # --- ИЗМЕНЕНО: Добавлен лок для атомарной проверки и установки статуса ---
    async with SPAM_STATUS_LOCK:
        if SPAM_STATUS.get(user_id, False):
            logger.warning(f"{log_prefix}: Spam task is already running for user {user_id}. Skipping this run.")
            return
        SPAM_STATUS[user_id] = True

    try:
        task_params = json.loads(task_params_json)
        session_limit = task_params.get('session_limit')

        is_attack_active = False # Scheduled tasks don't know about other task types
        status_message = await bot.send_message(user_id, f"🗓️ <b>Планировщик:</b> Запуск задачи 'Спам в группы'...", reply_markup=tasks_keyboard(is_spam_active=True, is_attack_active=is_attack_active))
        stop_event = asyncio.Event()
        STOP_EVENTS[user_id] = stop_event

        task = asyncio.create_task(spam_loop_task(user_id=user_id, bot=bot, status_chat_id=status_message.chat.id, status_message_id=status_message.message_id, session_limit=session_limit))
        ACTIVE_SPAM_TASKS[user_id] = task
        logger.info(f"{log_prefix}: Spam task successfully started for user {user_id}.")

    except Exception as e:
        logger.error(f"{log_prefix}: Failed to start scheduled spam task for user {user_id}: {e}", exc_info=True)
        # --- ИЗМЕНЕНО: Сбрасываем статус в случае ошибки запуска ---
        async with SPAM_STATUS_LOCK:
            SPAM_STATUS[user_id] = False
        await bot.send_message(user_id, f"❌ Не удалось запустить запланированную задачу спама. Ошибка: {e}")


async def run_scheduled_attack(bot: Bot, user_id: int, job_id: str, task_params_json: str):
    """
    This function is executed by the scheduler for an 'attack' task.
    """
    # --- ИЗМЕНЕНО: Локальный импорт для разрыва циклической зависимости ---
    from bot.client_tasks.client_manager import (
        ACTIVE_ATTACK_TASKS, ATTACK_STATUS, ATTACK_STATUS_LOCK,
        ATTACK_STOP_EVENTS, SPAM_STATUS
    )
    # --- ИЗМЕНЕНО: Локальный импорт для разрыва циклической зависимости ---
    from bot.client_tasks.attack_loop import attack_loop_task

    log_prefix = f"SCHEDULER_JOB_ATTACK [{job_id}]"
    logger.info(f"{log_prefix}: Triggered for user {user_id}.")

    # --- ИЗМЕНЕНО: Добавлен лок для атомарной проверки и установки статуса ---
    async with ATTACK_STATUS_LOCK:
        if ATTACK_STATUS.get(user_id, False):
            logger.warning(f"{log_prefix}: Attack task is already running for user {user_id}. Skipping this run.")
            return
        ATTACK_STATUS[user_id] = True

    try:
        task_params = json.loads(task_params_json)
        attack_mode = task_params.get('attack_mode', 'single')
        target_display = "по собранной базе" if attack_mode == 'mass' else f"на <code>{task_params.get('target_nickname', 'N/A')}</code>"

        is_spam_active = SPAM_STATUS.get(user_id, False)
        await bot.send_message(
            user_id,
            f"🗓️ <b>Планировщик:</b> Запуск задачи 'Атака в ЛС' {target_display}...",
            reply_markup=tasks_keyboard(is_spam_active=is_spam_active, is_attack_active=True)
        )
        stop_event = asyncio.Event()
        ATTACK_STOP_EVENTS[user_id] = stop_event

        task = asyncio.create_task(attack_loop_task(
            user_id=user_id, bot=bot, attack_mode=attack_mode,
            target_nickname=task_params.get('target_nickname'),
            message_count=task_params.get('message_count', 1),
            attack_delay=task_params.get('attack_delay', 1.5),
            use_ai=task_params.get('use_ai', False),
            is_infinite=False,  # Scheduled tasks are never infinite
            session_limit=task_params.get('session_limit')
        ))
        ACTIVE_ATTACK_TASKS[user_id] = task
        logger.info(f"{log_prefix}: Attack task successfully started for user {user_id}.")

    except Exception as e:
        logger.error(f"{log_prefix}: Failed to start scheduled attack task for user {user_id}: {e}", exc_info=True)
        # --- ИЗМЕНЕНО: Сбрасываем статус в случае ошибки запуска ---
        async with ATTACK_STATUS_LOCK:
            ATTACK_STATUS[user_id] = False
        await bot.send_message(user_id, f"❌ Не удалось запустить запланированную атаку. Ошибка: {e}")