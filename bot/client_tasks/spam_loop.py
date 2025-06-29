# bot/client_tasks/spam_loop.py
import asyncio
import html
import itertools
import logging
import os
import random
import time
from collections import Counter, deque
from typing import Optional

from aiogram import Bot
from pyrogram import Client
from pyrogram.enums import ChatType
from pyrogram.errors import (
    AuthKeyUnregistered, ChannelPrivate, ChatWriteForbidden, FloodWait,
    InviteHashExpired, PeerIdInvalid, SlowmodeWait, UserAlreadyParticipant,
    UserChannelsTooMuch, UserDeactivated, UsernameInvalid
)

import config
from bot.client_tasks.client_manager import (
    ACTIVE_SPAM_TASKS, ATTACK_STATUS, ATTACK_STATUS_LOCK, SESSION_MUTE_LOCK,
    SESSION_MUTE_UNTIL, SPAM_COOLDOWN_LOCK, SPAM_COOLDOWN_UNTIL, SPAM_STATS,
    SPAM_STATUS, SPAM_STATUS_LOCK, STOP_EVENTS, get_connected_client
)
from bot.client_tasks.task_utils import get_unique_text_with_fallback, record_worker_session_failure
from bot.database.db_manager import db_manager
from bot.keyboards import tasks_keyboard
from bot.utils.proxy_parser import parse_proxy_string

logger = logging.getLogger(__name__)


async def _spam_worker(
    user_id: int, bot: Bot, session_name: str, phone_for_log: str,
    chat_queue: asyncio.Queue, stop_event: asyncio.Event,
    comment_texts: list, delay: int, ai_settings: dict,
    stats_lock: asyncio.Lock, num_workers: int, is_persistent: bool,
    photo_file_path: Optional[str] = None, proxy: Optional[dict] = None
):
    """
    Задача-воркер для одной сессии.
    Отвечает за подключение, отправку сообщений и обработку ошибок Pyrogram.
    """
    logger.info(f"WORKER [{phone_for_log}]: Запущен (всего воркеров: {num_workers}).")
    
    client = None
    log_prefix = f"WORKER [{phone_for_log}]"
    try:
        client = await get_connected_client(user_id, session_name, no_updates=True, proxy=proxy)
        if not client:
            await record_worker_session_failure(
                user_id, phone_for_log, "Не удалось подключиться", stats_lock,
                SPAM_STATS, log_prefix, bot=bot
            )
            return

        # --- ИЗМЕНЕНО: Усиленная проверка и принудительное заполнение client.me ---
        # Это необходимо, чтобы избежать ошибки 'NoneType' object has no attribute 'is_premium'
        # при отправке медиа, так как Pyrogram не всегда надежно заполняет client.me.
        me = await client.get_me()
        if not me:
            logger.warning(f"{log_prefix}: Не удалось получить данные о себе (get_me() failed). Завершаю воркер.")
            await record_worker_session_failure(user_id, phone_for_log, "Не удалось получить данные о себе", stats_lock, SPAM_STATS, log_prefix, bot=bot)
            return
        
        # Принудительно устанавливаем атрибут, чтобы он был доступен во внутренних методах Pyrogram
        client.me = me
            
        if num_workers > 1:
            initial_stagger = random.uniform(0.5, delay if delay > 1 else 1.0)
            logger.info(f"{log_prefix}: Начальная задержка {initial_stagger:.2f} сек.")
            await asyncio.sleep(initial_stagger)

        effective_delay = delay * num_workers

        while not stop_event.is_set():
            # --- ИЗМЕНЕНО: Добавляем проверку на глобальный кулдаун в начале каждой итерации ---
            async with SPAM_COOLDOWN_LOCK:
                cooldown_end = SPAM_COOLDOWN_UNTIL.get(user_id, 0)

            current_time = time.time()
            if current_time < cooldown_end:
                sleep_duration = cooldown_end - current_time
                logger.info(f"{log_prefix}: Спам на глобальной паузе из-за FloodWait. Засыпаю на {sleep_duration:.1f} сек.")
                await asyncio.sleep(sleep_duration)

            try:
                # Получаем чат из общей очереди
                chat_identifier = await chat_queue.get()
            except asyncio.CancelledError:
                # Если основная задача отменяет воркер, выходим
                break

            # --- ИЗМЕНЕНО: Обертка в try...finally для гарантированного вызова task_done() ---
            try:
                # --- Основная логика обработки чата ---
                try:
                    chat = await client.join_chat(chat_identifier)
                except UserAlreadyParticipant:
                    chat = await client.get_chat(chat_identifier)

                if chat.type == ChatType.CHANNEL:
                    raise TypeError("Идентификатор является каналом, а не группой.")

                original_comment_text = random.choice(comment_texts)
                text_to_send = original_comment_text

                if ai_settings.get("enabled") and ai_settings.get("api_key"):
                    text_to_send = await get_unique_text_with_fallback(
                        original_text=original_comment_text,
                        user_id=user_id,
                        ai_settings=ai_settings,
                        stats_lock=stats_lock,
                        stats_dict=SPAM_STATS,
                        log_prefix=f"{log_prefix} ({chat_identifier})"
                    )

                # --- NEW: Add a check for valid file path ---
                photo_to_send = None
                if photo_file_path:
                    if os.path.exists(photo_file_path):
                        photo_to_send = photo_file_path
                    else:
                        logger.warning(f"{log_prefix}: Photo path '{photo_file_path}' not found on disk. Sending without photo. Please re-upload the photo in settings.")

                if photo_to_send:
                    logger.info(f"{log_prefix}: -> {chat_identifier} с ФОТО и текстом: \"{text_to_send[:30]}...\"")
                    await client.send_photo(
                        chat_id=chat.id,
                        photo=photo_to_send,
                        caption=text_to_send
                    )
                else:
                    logger.info(f"{log_prefix}: -> {chat_identifier} с текстом: \"{text_to_send[:30]}...\"")
                    await client.send_message(chat_id=chat.id, text=text_to_send)

                async with stats_lock:
                    if user_id in SPAM_STATS: SPAM_STATS[user_id]["messages"] += 1
                logger.info(f"{log_prefix}: Сообщение в {chat_identifier} ОТПРАВЛЕНО.")
                await asyncio.sleep(effective_delay)

            except (ChatWriteForbidden, InviteHashExpired, ChannelPrivate, TypeError, PeerIdInvalid) as e:
                error_type_name = type(e).__name__
                logger.warning(f"WORKER [{phone_for_log}]: Постоянная ошибка в {chat_identifier} ({error_type_name}). Удаляю чат.")
                async with stats_lock:
                    if user_id in SPAM_STATS:
                        SPAM_STATS[user_id]["errors"] += 1
                        SPAM_STATS[user_id]["error_details"].append(f"{chat_identifier}: {error_type_name}")
                await db_manager.delete_chat(user_id, chat_identifier)
            except SlowmodeWait as e:
                logger.warning(f"{log_prefix}: SlowMode в {chat_identifier}. Ожидаю {e.value} сек.")
                # Возвращаем чат в очередь и сразу ждем, чтобы не блокировать другие воркеры
                await asyncio.sleep(e.value)
                await chat_queue.put(chat_identifier) # Возвращаем чат в очередь
            except FloodWait as e:
                # --- ИЗМЕНЕНО: Воркер больше не завершается, а уходит на перерыв ---
                wait_seconds = e.value
                logger.warning(f"{log_prefix}: FloodWait ({wait_seconds} сек). Весь спам для пользователя {user_id} будет приостановлен.")

                cooldown_until = time.time() + wait_seconds + 5
                async with SPAM_COOLDOWN_LOCK:
                    SPAM_COOLDOWN_UNTIL[user_id] = cooldown_until
                async with SESSION_MUTE_LOCK:
                    SESSION_MUTE_UNTIL[session_name] = cooldown_until

                logger.info(f"{log_prefix}: Возвращаю чат {chat_identifier} в очередь.")
                await chat_queue.put(chat_identifier)
                continue # Переходим к следующей итерации, где сработает проверка кулдауна
            except UsernameInvalid:
                logger.warning(f"{log_prefix}: Не удалось обработать {chat_identifier} (UsernameInvalid). Возможно, сессия ограничена. Worker завершается.")
                notification_text = (
                    f"⚠️ <b>Проблема с сессией</b>\n\n"
                    f"Сессия <code>{html.escape(phone_for_log)}</code> не смогла обработать группу (ошибка <code>UsernameInvalid</code>). "
                    f"Это часто означает, что на аккаунт наложены <b>ограничения Telegram</b>.\n\n"
                    f"ℹ️ Воркер для этой сессии остановлен."
                )
                await record_worker_session_failure(
                    user_id, phone_for_log, "Ошибка разрешения юзернейма (ограничения)",
                    stats_lock, SPAM_STATS, log_prefix, bot=bot, notify_user=True,
                    notification_text=notification_text
                )
                break
            except Exception as e:
                error_type_name = type(e).__name__
                logger.error(f"{log_prefix}: Неизвестная ошибка в {chat_identifier} ({error_type_name}). Возвращаю в очередь.", exc_info=True)
                async with stats_lock:
                    if user_id in SPAM_STATS:
                        SPAM_STATS[user_id]["errors"] += 1
                        SPAM_STATS[user_id]["error_details"].append(f"{chat_identifier}: {error_type_name}")
                await chat_queue.put(chat_identifier) # Возвращаем чат в очередь
            finally:
                # Сообщаем очереди, что задача обработки этого элемента завершена
                chat_queue.task_done()
                logger.debug(f"{log_prefix}: task_done() вызван для чата {chat_identifier}")

    except asyncio.CancelledError:
        logger.info(f"{log_prefix}: Получен сигнал отмены.")
    except (AuthKeyUnregistered, UserDeactivated) as e:
        error_name = type(e).__name__
        reason_text = "сессия стала недействительной" if isinstance(e, AuthKeyUnregistered) else "аккаунт был удален"
        logger.error(f"{log_prefix}: Неработоспособен ({error_name}). Удаляю сессию.")
        notification_text = f"🗑️ <b>Сессия удалена</b>\n\nСессия <code>{html.escape(phone_for_log)}</code> была автоматически удалена, так как {reason_text}."
        await record_worker_session_failure(
            user_id, phone_for_log, f"{error_name} (удалена)", stats_lock, SPAM_STATS,
            log_prefix, bot=bot, notify_user=True, notification_text=notification_text
        )
        await db_manager.delete_session(user_id, phone_for_log)
    except UserChannelsTooMuch:
        logger.error(f"{log_prefix}: Достигнут лимит каналов. Завершение работы.")
        notification_text = (
            f"⚠️ <b>Проблема с сессией</b>\n\n"
            f"Сессия <code>{html.escape(phone_for_log)}</code> не может вступать в новые группы, так как <b>достигла лимита каналов/групп</b>.\n\n"
            f"ℹ️ Воркер для этой сессии остановлен."
        )
        await record_worker_session_failure(
            user_id, phone_for_log, "Достигнут лимит каналов", stats_lock, SPAM_STATS,
            log_prefix, bot=bot, notify_user=True, notification_text=notification_text
        )
    except Exception as e:
        logger.critical(f"{log_prefix}: Критическая ошибка: {e}", exc_info=True)
        await record_worker_session_failure(
            user_id, phone_for_log, f"Критическая ошибка: {type(e).__name__}",
            stats_lock, SPAM_STATS, log_prefix, bot=bot
        )
    finally:
        if client and client.is_connected:
            try:
                await asyncio.wait_for(client.disconnect(), timeout=10.0)
            except asyncio.TimeoutError:
                logger.warning(f"{log_prefix}: Отключение клиента заняло > 10 сек.")
            except Exception as e:
                logger.error(f"{log_prefix}: Ошибка при отключении клиента: {e}")
        logger.info(f"{log_prefix}: Завершил работу.")


async def _leave_worker(user_id: int, session_name: str, phone_for_log: str, chats_to_leave: list, proxy: Optional[dict]):
    """
    Задача-воркер для одной сессии, чтобы покинуть указанные чаты.
    """
    logger.info(f"LEAVER [{phone_for_log}]: Запущен для выхода из {len(chats_to_leave)} чатов.")
    client = None
    try:
        client = await get_connected_client(user_id, session_name, no_updates=True, proxy=proxy)
        if not client:
            logger.warning(f"LEAVER [{phone_for_log}]: Не удалось подключиться, выход не выполнен.")
            return

        for chat_identifier in chats_to_leave:
            try:
                # delete=True также удаляет чат из списка диалогов
                await client.leave_chat(chat_identifier, delete=True)
                logger.info(f"LEAVER [{phone_for_log}]: Успешно покинул чат {chat_identifier}.")
            except Exception:
                # Игнорируем ошибки, т.к. сессия могла и не быть в чате, или чат недействителен
                pass
            await asyncio.sleep(random.uniform(1, 2)) # Небольшая задержка
    except Exception as e:
        logger.error(f"LEAVER [{phone_for_log}]: Критическая ошибка в воркере выхода: {e}", exc_info=True)
    finally:
        if client and client.is_connected:
            await client.disconnect()
        logger.info(f"LEAVER [{phone_for_log}]: Завершил работу.")

async def spam_loop_task(
    user_id: int, bot: Bot, session_limit: Optional[int]
):
    """
    Основная задача-диспетчер для запуска и управления спам-циклом.
    """
    logger.info(f"DISPATCHER [{user_id}]: Запуск спам-цикла.")
    
    workers = []
    # --- ИЗМЕНЕНО: Инициализация Lock'а до блока try для безопасности в finally ---
    stats_lock = asyncio.Lock()
    active_sessions = {} # Определяем здесь для доступа в finally
    # --- ИЗМЕНЕНО: Инициализируем переменные, используемые в finally, до блока try ---
    ai_settings = {}
    proxies = []
    chat_count = 0

    try:
        # 1. Получение всех настроек из БД
        user_config = await db_manager.get_user_data(user_id)
        # Для спама используем только Pyrogram сессии
        all_user_sessions = await db_manager.get_sessions_by_type(user_id, 'pyrogram')
        proxies_list_str = user_config['proxies']
        comment_texts = await db_manager.get_comments(user_id)
        delay_seconds = await db_manager.get_delay(user_id)
        ai_settings = await db_manager.get_ai_settings(user_id)
        photo_file_path = await db_manager.get_spam_photo(user_id)

        # Local import to break circular dependency
        from bot.client_tasks.client_manager import (
            RESERVED_SESSIONS,
            RESERVED_SESSIONS_LOCK
        )

        # 2. Проверка на возможность запуска и РЕЗЕРВИРОВАНИЕ СЕССИЙ
        async with RESERVED_SESSIONS_LOCK:
            # Initialize user's reserved dict if not present
            if user_id not in RESERVED_SESSIONS:
                RESERVED_SESSIONS[user_id] = {}
            
            reserved_for_user = RESERVED_SESSIONS.get(user_id, {})
            
            # Find sessions that are not in mute and not reserved
            eligible_sessions = {}
            for phone, session_file_path in all_user_sessions.items():
                s_name = os.path.splitext(os.path.basename(session_file_path))[0]
                
                # Check mute
                mute_expires_at = SESSION_MUTE_UNTIL.get(s_name)
                if mute_expires_at and time.time() < mute_expires_at:
                    logger.info(f"DISPATCHER [{user_id}]: Сессия {s_name} в муте. Пропускается.")
                    continue
                
                # Check reservation
                if s_name in reserved_for_user:
                    logger.info(f"DISPATCHER [{user_id}]: Сессия {s_name} зарезервирована для '{reserved_for_user[s_name]}'. Пропускается.")
                    continue
                
                eligible_sessions[phone] = s_name

            session_items = list(eligible_sessions.items())
            random.shuffle(session_items)
            
            if session_limit is not None and session_limit > 0:
                session_items = session_items[:session_limit]

            active_sessions = dict(session_items) # These are the sessions we will use
            for phone, s_name in active_sessions.items():
                RESERVED_SESSIONS[user_id][s_name] = 'spam'
            logger.info(f"DISPATCHER [{user_id}]: Зарезервировано {len(active_sessions)} сессий для спама.")

        proxies = []
        if ai_settings.get("use_proxy", True):
            proxies = [parse_proxy_string(p) for p in proxies_list_str]
            proxies = [p for p in proxies if p]  # Filter out invalid ones
            logger.info(f"DISPATCHER [{user_id}]: Найдено {len(proxies)} валидных прокси для использования.")

        num_workers = len(active_sessions)
        logger.info(f"DISPATCHER [{user_id}]: Найдено {num_workers} активных сессий после проверки мута.")

        # Создаем и наполняем очередь задач из стрима БД
        chat_queue = asyncio.Queue()
        async for chat in db_manager.get_chats_stream(user_id):
            await chat_queue.put(chat)
            chat_count += 1

        if not active_sessions or chat_count == 0 or not comment_texts:
            logger.warning(f"DISPATCHER [{user_id}]: Спам-цикл прерван: нет активных сессий/групп/комментариев.")
            is_attack = ATTACK_STATUS.get(user_id, False)
            error_parts = []
            if not active_sessions: error_parts.append("активные сессии")
            if chat_count == 0: error_parts.append("группы")
            if not comment_texts: error_parts.append("тексты сообщений")
            error_text = f"❌ Не удалось запустить спам: отсутствуют {', '.join(error_parts)}."
            await bot.send_message(
                user_id, error_text,
                reply_markup=tasks_keyboard(is_spam_active=False, is_attack_active=is_attack)
            )
            return # Выходим, так как блок finally обработает очистку
        else:
            # 3. Инициализация состояния, если запуск возможен
            stop_event = STOP_EVENTS.get(user_id)
            SPAM_STATS[user_id] = {
                "messages": 0, "errors": 0,
                "sessions_initial_count": num_workers,
                "failed_sessions": [],
                "error_details": []
            }

            # 4. Создание дочерних задач
            workers = []
            proxy_cycle = itertools.cycle(proxies) if proxies else None
            for phone, s_name in active_sessions.items():
                assigned_proxy = next(proxy_cycle) if proxy_cycle else None
                # Создаем задачу для каждого воркера
                task = asyncio.create_task(_spam_worker(
                    user_id=user_id, bot=bot, session_name=s_name, phone_for_log=phone,
                    chat_queue=chat_queue, stop_event=stop_event,
                    comment_texts=comment_texts, delay=delay_seconds, ai_settings=ai_settings,
                    stats_lock=stats_lock, num_workers=num_workers,
                    is_persistent=ai_settings.get("persistent_spam", False),
                    photo_file_path=photo_file_path, proxy=assigned_proxy
                ), name=f"SpamWorker-{user_id}-{phone}")
                workers.append(task)

        # Создаем задачи-наблюдатели
        queue_waiter_task = asyncio.create_task(chat_queue.join(), name=f"QueueWaiter-{user_id}")
        stop_waiter_task = asyncio.create_task(stop_event.wait(), name=f"StopWaiter-{user_id}")

        # 5. Основной цикл ожидания
        logger.info(f"DISPATCHER [{user_id}]: Ожидание завершения работы или сигнала стоп.")
        done, pending = await asyncio.wait(
            {queue_waiter_task, stop_waiter_task},
            return_when=asyncio.FIRST_COMPLETED
        )

        # 6. Обработка завершения
        if stop_waiter_task in done:
            logger.info(f"DISPATCHER [{user_id}]: Получен сигнал остановки. Завершаем работу...")
        elif queue_waiter_task in done:
            logger.info(f"DISPATCHER [{user_id}]: Все чаты обработаны. Завершаем работу...")

        # Вне зависимости от причины, отменяем все еще работающие задачи
        logger.info(f"DISPATCHER [{user_id}]: Отмена всех оставшихся дочерних задач.")
        # Сначала отменяем воркеры, чтобы они перестали брать задачи из очереди
        for worker_task in workers:
            worker_task.cancel()
        # Затем отменяем остальные вспомогательные задачи
        for task in pending:
            task.cancel()
        
        # Собираем результаты отмененных задач, чтобы дать им время завершиться
        await asyncio.gather(*workers, *pending, return_exceptions=True)
        logger.info(f"DISPATCHER [{user_id}]: Все дочерние задачи завершены.")

    except asyncio.CancelledError:
        # Этот блок сработает, если сам spam_loop_task будет отменен извне (например, при выключении бота)
        logger.warning(f"DISPATCHER [{user_id}]: Задача спама была отменена извне. Принудительная очистка.")
        raise # Перевыбрасываем, чтобы внешний код (main.py) знал об отмене
    except Exception as e:
        # --- ДОБАВЛЕНО: Обработка других исключений для корректного завершения ---
        logger.critical(f"DISPATCHER [{user_id}]: Критическая ошибка в цикле спама: {e}", exc_info=True)
        # Блок finally все равно выполнится

    finally:
        # 7. Финальный отчет и очистка
        logger.info(f"DISPATCHER [{user_id}]: Вход в блок finally. Отправка отчета и очистка.")

        # --- ИЗМЕНЕНО: Очищаем глобальный кулдаун при завершении задачи ---
        async with SPAM_COOLDOWN_LOCK:
            SPAM_COOLDOWN_UNTIL.pop(user_id, None)

        # --- ИЗМЕНЕНО: Освобождение зарезервированных сессий ---
        if active_sessions:
            async with RESERVED_SESSIONS_LOCK:
                if user_id in RESERVED_SESSIONS:
                    sessions_to_release = list(active_sessions.values())
                    released_count = 0
                    for s_name in sessions_to_release:
                        if RESERVED_SESSIONS[user_id].pop(s_name, None):
                            released_count += 1
                    logger.info(f"DISPATCHER [{user_id}]: Освобождено {released_count} сессий из-под задачи 'спам'.")

        # Гарантированная отмена всех дочерних задач, если они еще существуют
        # --- ИЗМЕНЕНО: Проверяем, что stop_waiter_task определен ---
        tasks_to_cancel_base = workers
        if 'stop_waiter_task' in locals() and stop_waiter_task: tasks_to_cancel_base.append(stop_waiter_task)
        tasks_to_cancel = [t for t in tasks_to_cancel_base if t and not t.done()]
        if tasks_to_cancel:
            logger.warning(f"DISPATCHER [{user_id}]: Обнаружены незавершенные задачи в блоке finally. Принудительная отмена.")
            for task in tasks_to_cancel:
                task.cancel()
            await asyncio.gather(*tasks_to_cancel, return_exceptions=True)

        # Очистка глобального состояния
        async with SPAM_STATUS_LOCK:
            SPAM_STATUS[user_id] = False
        STOP_EVENTS.pop(user_id, None)
        ACTIVE_SPAM_TASKS.pop(user_id, None)
        final_stats = {}
        async with stats_lock:
            final_stats = SPAM_STATS.pop(user_id, {})
        
        # Формирование и отправка итогового отчета
        report_message = f"<b>🏁 Спам-сессия в группы завершена.</b>\n\n<b>📈 Статистика:</b>\n"
        report_message += f"  - Отправлено сообщений: {final_stats.get('messages', 0)}\n"
        report_message += f"  - Всего ошибок: {final_stats.get('errors', 0)}\n"
        
        failed = final_stats.get("failed_sessions", [])
        if failed:
            report_message += "\n<b>⚠️ Проблемные сессии:</b>\n"
            for f in failed:
                report_message += f"  - <code>{html.escape(f['phone'])}</code>: {html.escape(f['reason'])}\n"

        other_errors = final_stats.get("error_details", [])
        if other_errors:
            report_message += "\n<b>📋 Детализация прочих ошибок:</b>\n"
            error_counts = Counter(other_errors)
            for reason, count in error_counts.items():
                report_message += f"  - {html.escape(reason)} ({count} раз)\n"

        # --- Логика автовыхода ---
        if ai_settings.get("auto_leave_enabled", False) and chat_count > 0 and active_sessions:
            # Load chats only when needed for leaving, to save memory during the main loop
            target_chats_to_leave = [chat async for chat in db_manager.get_chats_stream(user_id)]
            if not target_chats_to_leave:
                logger.info(f"DISPATCHER [{user_id}]: Автовыход включен, но не найдено групп для выхода.")
            else:
                logger.info(f"DISPATCHER [{user_id}]: Запуск автовыхода из {len(target_chats_to_leave)} групп.")
                report_message += f"\n<b>📤 Автовыход:</b>\n  - Запущена попытка выхода из {len(target_chats_to_leave)} групп...\n"
                
                leave_workers = []
                proxy_cycle = itertools.cycle(proxies) if proxies else None
                for phone, s_name in active_sessions.items():
                    assigned_proxy = next(proxy_cycle) if proxy_cycle else None
                    leave_workers.append(
                        _leave_worker(user_id, s_name, phone, target_chats_to_leave, assigned_proxy)
                    )
                await asyncio.gather(*leave_workers, return_exceptions=True)
                logger.info(f"DISPATCHER [{user_id}]: Все воркеры автовыхода завершили работу.")

        is_attack = ATTACK_STATUS.get(user_id, False)
        
        try:
            await bot.send_message(
                user_id,
                report_message,
                reply_markup=tasks_keyboard(is_spam_active=False, is_attack_active=is_attack)
            )
        except Exception as e:
            logger.critical(f"DISPATCHER [{user_id}]: Не удалось отправить финальный отчет: {e}", exc_info=True)

        logger.info(f"DISPATCHER [{user_id}]: Спам-цикл полностью завершен.")