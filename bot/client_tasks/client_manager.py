# bot/client_tasks/client_manager.py
import asyncio
import logging
import os
import random
import sqlite3  # Для отлова специфичной ошибки поврежденной сессии
import time
from typing import Optional

from pyrogram import Client
from pyrogram.errors import AuthKeyUnregistered, UserDeactivated

import config
from bot.database.db_manager import db_manager

logger = logging.getLogger(__name__)

# --- Global State Variables ---
# These dictionaries and events hold the current status and control signals for spam and attack tasks.
# They are shared across different modules to manage the bot's operational state.

# Status flags for spam and attack operations for each user
SPAM_STATUS: dict[int, bool] = {}
ATTACK_STATUS: dict[int, bool] = {}
WARMER_STATUS: dict[int, bool] = {}

# --- ДОБАВЛЕНО: Локи для атомарного изменения статусов во избежание race conditions ---
SPAM_STATUS_LOCK: asyncio.Lock = asyncio.Lock()
ATTACK_STATUS_LOCK: asyncio.Lock = asyncio.Lock()
WARMER_STATUS_LOCK: asyncio.Lock = asyncio.Lock()

# Cooldowns to pause all workers for a task if one gets a FloodWait
SPAM_COOLDOWN_UNTIL: dict[int, float] = {}
SPAM_COOLDOWN_LOCK: asyncio.Lock = asyncio.Lock()
ATTACK_COOLDOWN_UNTIL: dict[int, float] = {}
ATTACK_COOLDOWN_LOCK: asyncio.Lock = asyncio.Lock()

# --- ДОБАВЛЕНО: Отслеживание зарезервированных сессий ---
RESERVED_SESSIONS: dict[int, dict[str, str]] = {} # {user_id: {session_name: task_type}}
RESERVED_SESSIONS_LOCK: asyncio.Lock = asyncio.Lock()

# Asyncio Events to signal stopping of spam/attack loops for specific users
STOP_EVENTS: dict[int, asyncio.Event] = {} # For spam
ATTACK_STOP_EVENTS: dict[int, asyncio.Event] = {} # For attack
WARMER_STOP_EVENTS: dict[int, asyncio.Event] = {}

# Statistics for ongoing spam/attack operations
SPAM_STATS: dict[int, dict] = {}
ATTACK_STATS: dict[int, dict] = {}
WARMER_STATS: dict[int, dict] = {}

# Lock and dictionary for managing session mute times (e.g., after FloodWait)
SESSION_MUTE_LOCK: asyncio.Lock = asyncio.Lock()
SESSION_MUTE_UNTIL: dict[str, float] = {}

# Tasks for active spam/attack operations, used for graceful shutdown
ACTIVE_SPAM_TASKS: dict[int, asyncio.Task] = {}
ACTIVE_ATTACK_TASKS: dict[int, asyncio.Task] = {}
ACTIVE_WARMER_TASKS: dict[int, asyncio.Task] = {}

# --- Session Validation Cache ---
SESSION_VALIDATION_CACHE: dict[int, dict] = {}
SESSION_VALIDATION_CACHE_LOCK: asyncio.Lock = asyncio.Lock()
VALIDATION_CACHE_TTL: int = 300  # 5 минут

# --- Логика подключения клиента ---

async def get_connected_client(
    # fmt: off
    user_id: int, session_name: str, no_updates: bool = False, proxy: Optional[dict] = None
) -> Optional[Client]:
    """
    Создает, подключает и возвращает клиент Pyrogram. Обрабатывает ошибки
    подключения, включая поврежденные сессии и временные блокировки БД.
    Accepts an optional proxy dictionary.
    """
    session_dir = os.path.join('sessions', str(user_id))

    client_params = {
        "name": session_name,
        "api_id": int(config.API_ID),
        "api_hash": config.API_HASH,
        "workdir": session_dir,
        "no_updates": no_updates,
    }
    if proxy:
        client_params["proxy"] = proxy
        logger.info(f"Connecting client {session_name} via proxy {proxy.get('hostname')}:{proxy.get('port')}")

    client = Client(**client_params)

    # --- ИЗМЕНЕНО: Логика get_me() перенесена в вызывающие функции. ---
    # Эта функция теперь отвечает только за подключение с повторными попытками.
    max_retries = 3
    try:
        for attempt in range(max_retries):
            try:
                await client.connect()
                return client  # Успешное подключение
            except (AuthKeyUnregistered, UserDeactivated) as e:
                # Фатальные ошибки авторизации, нет смысла повторять. Пробрасываем выше.
                raise e
            except sqlite3.OperationalError as e:
                if "database is locked" in str(e).lower() and attempt < max_retries - 1:
                    wait_time = random.uniform(0.5, 1.5)
                    logger.warning(
                        f"Session DB for {session_name} is locked (attempt {attempt + 1}/{max_retries}). "
                        f"Retrying in {wait_time:.2f}s..."
                    )
                    await asyncio.sleep(wait_time)
                    continue
                else:
                    # Последняя попытка или другая ошибка SQLite, пробрасываем выше.
                    raise e
            except Exception as e:
                # Другие ошибки подключения (сеть и т.д.)
                logger.warning(f"Connection failed for {session_name} (attempt {attempt + 1}/{max_retries}): {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(random.uniform(1.0, 2.0))
                    continue
                else:
                    # Последняя попытка, пробрасываем выше.
                    raise e
    except sqlite3.OperationalError as e:
        if "no such column" in str(e).lower() or "malformed" in str(e).lower():
            logger.error(f"Сессия {session_name} для {user_id} повреждена или несовместима ({e}). Автоматическое удаление...")
            try:
                if client.is_connected:
                    await client.disconnect()
            except ConnectionError:
                pass  # Игнорируем ошибку, если клиент уже был отключен

            await db_manager.delete_session(user_id, session_name)
            logger.info(f"Поврежденная сессия {session_name} удалена. Пользователю нужно будет добавить ее заново.")
            return None # Сигнал о сбое, который обработан здесь
        else:
            # Другие ошибки SQLite
            logger.critical(f"Непредвиденная/повторная ошибка SQLite для сессии {session_name}: {e}", exc_info=True)
            raise e # Пробрасываем, чтобы validate_user_sessions пометил как ошибку
    except Exception as e:
        # Сюда попадают ошибки после всех попыток или те, что мы пробросили выше (AuthKey, etc.)
        logger.error(f"Не удалось подключить клиента {session_name} для {user_id}: {e}", exc_info=True)
        try:
            if client.is_connected:
                await client.disconnect()
        except ConnectionError:
            pass # Игнорируем ошибку, если клиент уже был отключен
        raise e # Пробрасываем ошибку, чтобы вызывающий код мог ее обработать


# --- Логика валидации сессий ---

async def validate_user_sessions(user_id: int) -> list[dict]:
    """
    Проверяет все сессии пользователя на валидность и возвращает их статусы.
    Использует кэширование для снижения нагрузки.
    """
    # Сначала проверяем кэш
    async with SESSION_VALIDATION_CACHE_LOCK:
        cached_data = SESSION_VALIDATION_CACHE.get(user_id)
        if cached_data and time.time() < cached_data['expiry']:
            logger.info(f"CACHE HIT: Using cached session statuses for user {user_id}")
            return cached_data['statuses']

    user_data = await db_manager.get_user_data(user_id)
    sessions = user_data.get('sessions', {})
    statuses = []

    logger.info(f"CACHE MISS: Performing live session validation for user {user_id}")

    if not sessions:
        return []

    for phone, session_file_path in sessions.items():
        session_name = os.path.splitext(os.path.basename(session_file_path))[0]
        status_info = {'phone': phone, 'status': '❓ Проверка...'}
        client = None
        try:
            client = await get_connected_client(user_id, session_name, no_updates=True)
            if client:
                # Явно вызываем get_me() для проверки валидности сессии
                me = await client.get_me()
                if me:
                    status_info['status'] = f"✅ OK ({me.first_name})"
                else:
                    # Случай, когда сессия подключилась, но не смогла получить данные о себе
                    logger.warning(f"Сессия {session_name} для {user_id} подключилась, но get_me() не вернул данные. Считаем невалидной.")
                    status_info['status'] = "❌ Ошибка (нет данных)"
                    status_info['is_bad'] = True
            else:
                # get_connected_client вернул None (например, сессия повреждена и уже удалена)
                status_info['status'] = "❌ Ошибка (повреждена?)"
                status_info['is_bad'] = True
        except (AuthKeyUnregistered, UserDeactivated) as e:
            status_info['status'] = f"🚫 Невалидна ({type(e).__name__})"
            status_info['is_bad'] = True
        except Exception as e:
            logger.error(f"Ошибка при проверке сессии {phone}: {e}", exc_info=True)
            status_info['status'] = f"❌ Ошибка ({type(e).__name__})"
            status_info['is_bad'] = True
        finally:
            try:
                if client and client.is_connected:
                    await client.disconnect()
            except ConnectionError:
                pass # Игнорируем ошибку, если клиент уже был отключен
            statuses.append(status_info)

    # Сохраняем свежие данные в кэш
    async with SESSION_VALIDATION_CACHE_LOCK:
        SESSION_VALIDATION_CACHE[user_id] = {
            'statuses': statuses,
            'expiry': time.time() + VALIDATION_CACHE_TTL
        }

    return statuses