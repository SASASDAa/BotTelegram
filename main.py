# main.py
import asyncio
import logging
import os
import sys
import html
import traceback

# --- Настройка логгирования ---
# Это должно быть в самом верху, до импорта других модулей вашего приложения.
log_format = '%(asctime)s - PID:%(process)d - %(name)s - %(levelname)s - %(message)s'
# Использование basicConfig с force=True (для Python 3.8+) - это надежный способ
# установить конфигурацию логирования для всего приложения. Он удалит
# любые предыдущие обработчики и применит новые.
logging.basicConfig(level=logging.INFO, format=log_format, stream=sys.stdout, force=True)
# --- Конец настройки логгирования ---

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage

import config
# --- ДОБАВЛЕНО ---
from bot.client_tasks.client_manager import (
    ACTIVE_ATTACK_TASKS, ACTIVE_SPAM_TASKS, ACTIVE_WARMER_TASKS,
    ATTACK_STOP_EVENTS, STOP_EVENTS, WARMER_STOP_EVENTS
)
from bot.database.db_manager import db_manager
from bot.handlers import (
    admin, attack_handler, common, profile, scheduler_handler,
    scraper_handler, settings_ai, settings_chat, settings_comments, warmer_handler,
    settings_other, settings_proxy, settings_sessions, spam_handler
)
from bot.middlewares import AccessMiddleware
from bot.scheduler_manager import scheduler_manager, init_scheduler

logger = logging.getLogger(__name__)

async def perform_restart(bot: Bot):
    """Gracefully shuts down and restarts the bot process."""
    logger.info("Restart requested by admin. Shutting down...")
    await on_shutdown(bot)
    logger.info("Graceful shutdown complete. Executing restart...")
    # Using os.execv to replace the current process with a new one
    os.execv(sys.executable, ['python'] + sys.argv)

async def on_shutdown(bot: Bot):
    logger.info("Bot is shutting down. Signaling background tasks to stop...")
    all_tasks = []
    task_groups = [
        ("spam", ACTIVE_SPAM_TASKS, STOP_EVENTS),
        ("attack", ACTIVE_ATTACK_TASKS, ATTACK_STOP_EVENTS),
        ("warmer", ACTIVE_WARMER_TASKS, WARMER_STOP_EVENTS)
    ]

    for task_type, active_tasks_dict, stop_events_dict in task_groups:
        for user_id, task in list(active_tasks_dict.items()):
            if not task.done():
                logger.info(f"Signaling {task_type} task for user {user_id} to stop.")
                if user_id in stop_events_dict:
                    stop_events_dict[user_id].set()
                all_tasks.append(task)

    if all_tasks:
        logger.info(f"Waiting for {len(all_tasks)} background tasks to complete...")
        try:
            await asyncio.wait_for(asyncio.gather(*all_tasks, return_exceptions=True), timeout=30.0)
            logger.info("All background tasks finished gracefully.")
        except asyncio.TimeoutError:
            logger.warning("Graceful shutdown timed out. Forcing task cancellation.")
    
    if scheduler_manager:
        await scheduler_manager.shutdown()

    await bot.session.close()
    logger.info("Bot session closed. Shutdown complete.")

async def main():
    # Инициализация базы данных
    await db_manager.init_db()
    logger.info("Database initialized.")

    # Инициализация бота и диспетчера
    bot = Bot(token=config.BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    storage = MemoryStorage()
    dp = Dispatcher(storage=storage, restart_function=perform_restart)
    
    # --- Инициализация планировщика ---
    init_scheduler(bot)

    # --- ИЗМЕНЕНО: Регистрация обработчика корректного завершения ---
    dp.shutdown.register(on_shutdown)

    # --- ИЗМЕНЕНО: Регистрация middleware как "внутреннего" ---
    # Регистрируем middleware отдельно для сообщений и коллбэков.
    # Это более прямой способ, который должен сработать, если глобальный перехватчик не работает.
    access_middleware = AccessMiddleware(config.SUPER_ADMIN_ID)
    dp.message.middleware(access_middleware)
    dp.callback_query.middleware(access_middleware)

    # Регистрация роутеров
    logger.info("Registering routers...")
    dp.include_router(common.router)
    dp.include_router(profile.router)
    dp.include_router(admin.router)
    dp.include_router(scheduler_handler.router)
    dp.include_router(scraper_handler.router)
    dp.include_router(warmer_handler.router)
    dp.include_router(settings_sessions.router)
    dp.include_router(settings_chat.router)
    dp.include_router(settings_comments.router)
    dp.include_router(settings_ai.router)
    dp.include_router(settings_other.router)
    dp.include_router(settings_proxy.router)
    dp.include_router(spam_handler.router)
    dp.include_router(attack_handler.router)
    logger.info("Routers registered.")

    # --- Уведомление о запуске ---
    try:
        await bot.send_message(config.SUPER_ADMIN_ID, "✅ Бот успешно запущен/перезапущен и готов к работе.")
        logger.info(f"Sent startup notification to SUPER_ADMIN_ID {config.SUPER_ADMIN_ID}")
    except Exception as e:
        logger.error(f"Could not send startup notification to SUPER_ADMIN_ID {config.SUPER_ADMIN_ID}: {e}")

    # Удаление вебхука и запуск поллинга
    await bot.delete_webhook(drop_pending_updates=True)
    if scheduler_manager:
        await scheduler_manager.start()
    logger.info("Bot starting polling...")
    
    # --- ИЗМЕНЕНО: Добавлен глобальный обработчик ошибок ---
    # Этот блок отловит любую критическую, не обработанную в другом месте ошибку,
    # которая может "уронить" бота. Он отправит уведомление администратору
    # и позволит systemd корректно перезапустить процесс.
    try:
        await dp.start_polling(bot)
    except Exception as e:
        logger.critical(f"КРИТИЧЕСКАЯ НЕОБРАБОТАННАЯ ОШИБКА: {e}", exc_info=True)
        try:
            tb_str = traceback.format_exc()
            error_message = (
                f"🚨 <b>КРИТИЧЕСКАЯ ОШИБКА!</b> 🚨\n\n"
                f"Бот столкнулся с непредвиденной ошибкой и будет перезапущен сервером.\n\n"
                f"<b>Тип ошибки:</b>\n<code>{html.escape(type(e).__name__)}</code>\n\n"
                f"<b>Сообщение:</b>\n<code>{html.escape(str(e))}</code>\n\n"
                f"<b>Traceback (последние 1000 символов):</b>\n<pre>{html.escape(tb_str[-1000:])}</pre>"
            )
            await bot.send_message(config.SUPER_ADMIN_ID, error_message)
        except Exception as notify_error:
            logger.error(f"НЕ УДАЛОСЬ ОТПРАВИТЬ УВЕДОМЛЕНИЕ О КРИТИЧЕСКОЙ ОШИБКЕ: {notify_error}")


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped by user (KeyboardInterrupt/SystemExit).")