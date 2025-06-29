# bot/client_tasks/search.py
import asyncio
import itertools
import logging
import os
import random
import time

from aiogram import Bot
from pyrogram.enums import ChatType
from pyrogram.errors import FloodWait, AuthKeyUnregistered, UserDeactivated

from bot.client_tasks.client_manager import (
    ATTACK_STATUS, SPAM_STATUS, get_connected_client, SESSION_MUTE_LOCK, SESSION_MUTE_UNTIL
)
from bot.database.db_manager import db_manager
from bot.utils.proxy_parser import parse_proxy_string
# --- ИЗМЕНЕННЫЙ ИМПОРТ ---
# Теперь keyboards.py находится в том же пакете 'bot'
from bot.keyboards import main_keyboard

logger = logging.getLogger(__name__)

async def search_chats_task(bot: Bot, user_id: int, keywords: list):
    """Задача для поиска публичных групп с использованием сессий пользователя."""
    logger.info(f"Начало поиска групп для {user_id} по ключевым словам: {keywords}")
    # Используем только Pyrogram сессии для стабильности
    user_sessions_data = await db_manager.get_sessions_by_type(user_id, 'pyrogram')
    proxies_list_str = (await db_manager.get_user_data(user_id))['proxies']

    if not user_sessions_data:
        await bot.send_message(
            user_id,
            "❌ Нет сессий Pyrogram для поиска групп.",
            reply_markup=main_keyboard()
        )
        return

    ai_settings = await db_manager.get_ai_settings(user_id)
    proxies = []
    if ai_settings.get("use_proxy", True):
        proxies = [parse_proxy_string(p) for p in proxies_list_str]
        proxies = [p for p in proxies if p]
        logger.info(f"SEARCH: Найдено {len(proxies)} валидных прокси для использования.")

    found_chats_set = set()
    working_sessions_count = 0
    total_queries_processed = 0
    
    session_items = list(user_sessions_data.items())
    random.shuffle(session_items)
    proxy_cycle = itertools.cycle(proxies) if proxies else None

    for phone, session_file_path in session_items:
        client = None
        try:
            session_name = os.path.splitext(os.path.basename(session_file_path))[0]
            assigned_proxy = next(proxy_cycle) if proxy_cycle else None
            client = await get_connected_client(user_id, session_name, proxy=assigned_proxy)
            if not client:
                continue
            
            working_sessions_count += 1
            logger.info(f"Поиск ГРУПП: Использую сессию {phone}")
            
            for query in keywords:
                try:
                    # Поиск по глобальным результатам
                    async for peer in client.search_global(query=query, limit=50):
                        if hasattr(peer, 'chat') and peer.chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]:
                            if peer.chat.username:
                                found_chats_set.add(f"@{peer.chat.username}")

                    total_queries_processed += 1
                    await asyncio.sleep(random.uniform(5, 8)) # Задержка между ключевыми словами

                except FloodWait as e:
                    wait_time = e.value
                    logger.warning(f"Поиск ГРУПП FloodWait: {wait_time} сек на {phone} для запроса '{query}'. Сессия будет в муте.")
                    mute_until = time.time() + wait_time + 5
                    async with SESSION_MUTE_LOCK:
                        SESSION_MUTE_UNTIL[session_name] = mute_until
                    await asyncio.sleep(wait_time + 5)
                except Exception as e_inner:
                    logger.error(f"Поиск ГРУПП ошибка '{query}' сессией {phone}: {e_inner}")
            
            await asyncio.sleep(random.uniform(10, 15)) # Задержка между сессиями

        except (AuthKeyUnregistered, UserDeactivated) as e:
            logger.error(f"Сессия {phone} недействительна ({type(e).__name__}) и будет удалена.")
            await db_manager.delete_session(user_id, phone)
        except Exception as e_session:
            logger.error(f"Поиск ГРУПП: Общая ошибка с сессией {phone}: {e_session}")
        finally:
            if client and client.is_connected:
                await client.disconnect()

    if working_sessions_count == 0:
        await bot.send_message(
            user_id,
            "❌ Нет рабочих сессий для поиска групп.",
            reply_markup=main_keyboard()
        )
        return

    if found_chats_set:
        await db_manager.add_chats(user_id, list(found_chats_set))
        report_message = (
            f"✅ <b>Поиск групп завершен!</b>\n\n"
            f"Найдено и добавлено новых групп: {len(found_chats_set)}\n"
            f"Обработано запросов: {total_queries_processed}\n"
            f"Использовано сессий: {working_sessions_count}"
        )
        await bot.send_message(
            user_id,
            report_message,
            reply_markup=main_keyboard()
        )
    else:
        await bot.send_message(
            user_id,
            "❌ Поиск групп завершен. Ничего не найдено по вашим ключевым словам.",
            reply_markup=main_keyboard()
        )