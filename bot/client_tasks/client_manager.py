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
from telethon import TelegramClient
from telethon.errors import (
    AuthKeyUnregisteredError as TelethonAuthKeyUnregisteredError, InvalidBufferError,
    UserDeactivatedError as TelethonUserDeactivatedError
)

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

# --- Telethon FSM Clients ---
# This will be used for FSM-based authorization
FSM_TELETHON_CLIENTS: dict[int, TelegramClient] = {}

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

async def get_connected_telethon_client(
    user_id: int, session_name: str, proxy: Optional[dict] = None
) -> Optional[TelegramClient]:
    """
    Создает, подключает и возвращает клиент Telethon.
    """
    session_path = os.path.join('sessions', str(user_id), f"{session_name}.session")

    # --- ИЗМЕНЕНО: Логика загрузки сессии Telethon ---
    # Сессии Telethon в этом боте сохраняются как "string sessions" в файлах.
    # Их нужно прочитать и передать в конструктор как объект StringSession,
    # а не как путь к файлу, иначе Telethon попытается открыть их как SQLite базу,
    # что вызовет ошибку "file is not a database".
    session_instance = None
    try:
        if os.path.exists(session_path):
            with open(session_path, 'r') as f:
                session_string = f.read().strip()
            if session_string:
                from telethon.sessions import StringSession
                session_instance = StringSession(session_string)
            else:
                logger.warning(f"Файл сессии Telethon {session_path} пуст. Сессия не будет загружена.")
                return None
    except Exception as e:
        logger.error(f"Ошибка чтения файла сессии Telethon {session_path}: {e}")
        return None

    # Telethon proxy format is slightly different from Pyrogram's
    telethon_proxy = None
    if proxy:
        telethon_proxy = (
            proxy['scheme'],
            proxy['hostname'],
            proxy['port'],
            True, # RDNS
            proxy.get('username'),
            proxy.get('password')
        )

    client = TelegramClient(
        session=session_instance,
        api_id=int(config.API_ID),
        api_hash=config.API_HASH,
        proxy=telethon_proxy
    )

    try:
        logger.debug(f"Подключение Telethon клиента {session_name}...")
        await client.connect()
        # После успешного подключения обновляем файл сессии, т.к. ключ мог измениться
        if session_instance:
            new_session_string = client.session.save()
            with open(session_path, 'w') as f:
                f.write(new_session_string)

        return client
    except (TelethonAuthKeyUnregisteredError, TelethonUserDeactivatedError) as e:
        raise e # Пробрасываем выше для обработки в воркере
    except (sqlite3.DatabaseError, InvalidBufferError) as e:
        # Эта ошибка может возникнуть, если файл сессии поврежден или имеет неверный формат
        # (например, это файл Pyrogram, а не Telethon).
        logger.error(f"Сессия Telethon {session_name} для {user_id} повреждена или несовместима ({e}). Автоматическое удаление...")
        if client.is_connected():
            await client.disconnect()
        await db_manager.delete_session(user_id, session_name)
        return None
    except Exception as e:
        logger.error(f"Не удалось подключить Telethon клиента {session_name} для {user_id}: {e}", exc_info=True)
        if client.is_connected():
            await client.disconnect()
        raise e

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

    all_sessions_details = await db_manager.get_sessions_with_details(user_id)
    statuses = []

    logger.info(f"CACHE MISS: Performing live session validation for user {user_id}")

    if not all_sessions_details:
        return []

    for session_details in all_sessions_details:
        phone = session_details['phone']
        session_file_path = session_details['session_file']
        client_type = session_details['client_type']
        session_name = os.path.splitext(os.path.basename(session_file_path))[0]
        status_info = {'phone': phone, 'status': '❓ Проверка...', 'client_type': client_type}
        client = None

        if client_type == 'telethon':
            status_info['status'] = "✅ OK (Telethon)"
            statuses.append(status_info)
            continue

        try:
            # Для Pyrogram сессий оставляем старую логику
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