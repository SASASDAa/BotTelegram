# bot/client_tasks/broadcast.py
import asyncio
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest

from bot.database.db_manager import db_manager

logger = logging.getLogger(__name__)

async def broadcast_task(bot: Bot, admin_id: int, message_text: str):
    """
    Sends a message to all users in the database.
    """
    user_ids = await db_manager.get_all_user_ids()
    total_users = len(user_ids)
    sent_count = 0
    blocked_count = 0
    
    await bot.send_message(admin_id, f"📢 Начинаю рассылку для {total_users} пользователей...")
    
    start_time = asyncio.get_event_loop().time()
    
    for user_id in user_ids:
        try:
            await bot.send_message(user_id, message_text, disable_web_page_preview=True)
            sent_count += 1
        except TelegramForbiddenError:
            logger.info(f"Broadcast: User {user_id} has blocked the bot. Skipping.")
            blocked_count += 1
        except TelegramBadRequest as e:
            logger.warning(f"Broadcast: Could not send to {user_id}. Error: {e}")
        except Exception as e:
            logger.error(f"Broadcast: Unexpected error sending to {user_id}: {e}", exc_info=True)
        
        # To avoid hitting Telegram API limits
        if sent_count > 0 and sent_count % 25 == 0:
            await asyncio.sleep(1)
            
    end_time = asyncio.get_event_loop().time()
    duration = round(end_time - start_time, 2)
    
    report = (
        f"🏁 Рассылка завершена за {duration} сек.\n\n"
        f"▫️ Всего пользователей: {total_users}\n"
        f"▫️ Отправлено: {sent_count}\n"
        f"▫️ Заблокировали бота: {blocked_count}"
    )
    await bot.send_message(admin_id, report)