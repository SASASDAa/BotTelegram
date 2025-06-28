# bot/client_tasks/scraper.py
import asyncio
import logging
import os
import random
import time

from aiogram import Bot
from pyrogram.enums import UserStatus
from pyrogram.errors import (
    AuthKeyUnregistered, UserDeactivated, FloodWait, ChannelPrivate,
    UsernameNotOccupied
)

from bot.client_tasks.client_manager import (
    get_connected_client, SESSION_MUTE_LOCK, SESSION_MUTE_UNTIL
)
from bot.database.db_manager import db_manager
from bot.keyboards import settings_keyboard

logger = logging.getLogger(__name__)


async def scraper_task(bot: Bot, user_id: int, target_group: str):
    """Задача для сбора (парсинга) участников из целевой группы."""
    logger.info(f"SCRAPER: Начало сбора для {user_id} из группы {target_group}")
    
    user_data = await db_manager.get_user_data(user_id)
    if not user_data['sessions']:
        await bot.send_message(user_id, "❌ Не найдено сессий для запуска сбора.", reply_markup=settings_keyboard())
        return

    # Выбираем случайную сессию для работы
    phone, session_file_path = random.choice(list(user_data['sessions'].items()))
    session_name = os.path.splitext(os.path.basename(session_file_path))[0]
    
    client = None
    try:
        client = await get_connected_client(user_id, session_name, no_updates=True)
        if not client:
            await bot.send_message(user_id, f"❌ Не удалось подключиться к сессии {phone} для сбора.", reply_markup=settings_keyboard())
            return

        logger.info(f"SCRAPER: Использую сессию {phone} для сбора из {target_group}")
        
        found_users = []
        # --- ИЗМЕНЕНО: Убран неверный параметр 'filter' и добавлена проверка статуса пользователя ---
        # Параметр filter в get_chat_members ожидает ChatMembersFilter, а не UserStatus.
        # Фильтрация по "живым" пользователям (онлайн/недавно) выполняется вручную.
        async for member in client.get_chat_members(target_group):
            user = member.user
            if not user.is_bot and not user.is_deleted and user.status in [UserStatus.ONLINE, UserStatus.RECENTLY]:
                found_users.append({'id': user.id, 'username': user.username})
                if len(found_users) % 100 == 0:
                    logger.info(f"SCRAPER: Собрано {len(found_users)} участников из {target_group}...")
                    await asyncio.sleep(1) # Небольшая передышка

        if not found_users:
            await bot.send_message(user_id, f"ℹ️ В группе {target_group} не найдено активных участников или группа недоступна.", reply_markup=settings_keyboard())
            return

        await db_manager.add_scraped_users(user_id, target_group, found_users)
        total_count = await db_manager.get_scraped_users_count(user_id)

        report_message = (
            f"✅ <b>Сбор завершен!</b>\n\n"
            f"▫️ Группа: <code>{target_group}</code>\n"
            f"▫️ Найдено новых/активных: {len(found_users)}\n"
            f"▫️ Всего в вашей базе: {total_count}"
        )
        await bot.send_message(user_id, report_message, reply_markup=settings_keyboard())

    except (UsernameNotOccupied, ChannelPrivate):
        await bot.send_message(user_id, f"❌ Не удалось найти группу <code>{target_group}</code> или она является приватной.", reply_markup=settings_keyboard())
    except FloodWait as e:
        wait_time = e.value
        logger.warning(f"SCRAPER: FloodWait на {wait_time} сек для сессии {phone}. Сессия будет в муте.")
        mute_until = time.time() + wait_time + 5
        async with SESSION_MUTE_LOCK:
            SESSION_MUTE_UNTIL[session_name] = mute_until
        await bot.send_message(user_id, f"⚠️ Сессия {phone} получила FloodWait на {wait_time} секунд и временно не будет использоваться. Попробуйте позже.", reply_markup=settings_keyboard())
    except (AuthKeyUnregistered, UserDeactivated) as e:
        await bot.send_message(user_id, f"❌ Сессия {phone} недействительна ({type(e).__name__}). Удалите ее и добавьте заново.", reply_markup=settings_keyboard())
        await db_manager.delete_session(user_id, phone)
    except Exception as e:
        logger.error(f"SCRAPER: Критическая ошибка при сборе из {target_group} для {user_id}: {e}", exc_info=True)
        await bot.send_message(user_id, f"❌ Произошла непредвиденная ошибка: {e}", reply_markup=settings_keyboard())
    finally:
        if client and client.is_connected:
            await client.disconnect()