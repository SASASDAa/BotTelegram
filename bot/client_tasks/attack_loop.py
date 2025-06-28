# bot/client_tasks/attack_loop.py
import asyncio
import html
import itertools
import logging
import os
import random
import time
from collections import Counter
from typing import Optional

from aiogram import Bot
from pyrogram.enums import ChatMemberStatus, ChatType
from pyrogram.errors import (
    AuthKeyUnregistered, FloodWait, PeerFlood, UserAlreadyParticipant,
    UserDeactivated, UserPrivacyRestricted, UsernameNotOccupied,
    ChannelPrivate, UserNotParticipant, PeerIdInvalid
)

from bot.client_tasks.client_manager import (
    ATTACK_COOLDOWN_LOCK, ATTACK_COOLDOWN_UNTIL, ATTACK_STATS, ATTACK_STATUS,
    ATTACK_STATUS_LOCK, ATTACK_STOP_EVENTS, SESSION_MUTE_LOCK,
    SESSION_MUTE_UNTIL, SPAM_STATUS, get_connected_client
)
from bot.database.db_manager import db_manager
from bot.client_tasks.task_utils import (
    get_unique_text_with_fallback, record_worker_session_failure
)
from bot.utils.proxy_parser import parse_proxy_string
from bot.keyboards import tasks_keyboard

logger = logging.getLogger(__name__)


async def _attack_worker(
    bot: Bot, user_id: int, session_name: str, phone_for_log: str,
    target_queue: asyncio.Queue, message_count: int, attack_delay: float,
    use_ai: bool, comment_texts: list, ai_settings: dict, stop_event: asyncio.Event,
    stats_lock: asyncio.Lock, is_infinite: bool, photo_file_path: Optional[str] = None,
    proxy: Optional[dict] = None
):
    """Worker task for a single session to send DMs."""
    logger.info(f"Worker (атака в ЛС) для сессии {phone_for_log} запущен.")

    client = None
    log_prefix = f"Worker {phone_for_log} (атака в ЛС)"
    try:
        client = await get_connected_client(user_id, session_name, proxy=proxy)
        if not client:
            await record_worker_session_failure(
                user_id, phone_for_log, "Не удалось подключиться", stats_lock,
                ATTACK_STATS, log_prefix, bot=bot
            )
            return

        # --- ИЗМЕНЕНО: Усиленная проверка и принудительное заполнение client.me ---
        # Это необходимо, чтобы избежать ошибки 'NoneType' object has no attribute 'is_premium'
        # при отправке медиа, так как Pyrogram не всегда надежно заполняет client.me.
        me = await client.get_me()
        if not me:
            logger.warning(f"{log_prefix}: Не удалось получить данные о себе (get_me() failed). Завершаю воркер.")
            await record_worker_session_failure(user_id, phone_for_log, "Не удалось получить данные о себе", stats_lock, ATTACK_STATS, log_prefix, bot=bot)
            return
        
        # Принудительно устанавливаем атрибут, чтобы он был доступен во внутренних методах Pyrogram
        client.me = me

        while not stop_event.is_set():
            # --- ИЗМЕНЕНО: Добавляем проверку на глобальный кулдаун в начале каждой итерации ---
            async with ATTACK_COOLDOWN_LOCK:
                cooldown_end = ATTACK_COOLDOWN_UNTIL.get(user_id, 0)

            current_time = time.time()
            if current_time < cooldown_end:
                sleep_duration = cooldown_end - current_time
                logger.info(f"{log_prefix}: Атака на глобальной паузе из-за FloodWait. Засыпаю на {sleep_duration:.1f} сек.")
                await asyncio.sleep(sleep_duration)

            try:
                target_identifier = await target_queue.get()
            except asyncio.CancelledError:
                break

            # --- ИСПРАВЛЕНО: Обертка всей логики обработки в try...finally ---
            # Это гарантирует, что task_done() будет вызван для каждого элемента очереди,
            # независимо от того, как завершилась его обработка (успешно, с ошибкой, continue, break или return).
            try:
                target_user = None
                try:
                    # Resolve target identifier (can be username or ID)
                    target_user = await client.get_users(target_identifier)
                    if not target_user:
                        raise ValueError("Пользователь не найден")
                except Exception as e:
                    logger.warning(f"{log_prefix}: Не удалось найти цель '{target_identifier}': {e}. Пропускаю.")
                    async with stats_lock:
                        if user_id in ATTACK_STATS:
                            stats = ATTACK_STATS[user_id]
                            stats["errors"] += 1
                            stats["error_details"].append(f"Цель не найдена: {target_identifier}")
                    continue # Переходим к finally и следующему элементу в while

                target_log_name = f"@{target_user.username}" if target_user.username else str(target_user.id)
                message_iterator = itertools.count(1) if is_infinite else range(1, message_count + 1)

                for i in message_iterator:
                    if stop_event.is_set():
                        logger.info(f"{log_prefix}: Получен сигнал остановки. Возвращаю цель {target_log_name} в очередь.")
                        await target_queue.put(target_identifier)
                        break

                    original_comment_text = random.choice(comment_texts)
                    text_to_send = original_comment_text
                    if use_ai and ai_settings.get("enabled") and ai_settings.get("api_key"):
                        text_to_send = await get_unique_text_with_fallback(
                            original_text=original_comment_text,
                            user_id=user_id,
                            ai_settings=ai_settings,
                            stats_lock=stats_lock,
                            stats_dict=ATTACK_STATS,
                            log_prefix=f"{log_prefix} -> {target_log_name}"
                        )

                    try:
                        log_msg_count = f"(#{i})" if is_infinite else f"({i}/{message_count})"
                        
                        # --- NEW: Add a check for valid file path ---
                        photo_to_send = None
                        if photo_file_path:
                            if os.path.exists(photo_file_path):
                                photo_to_send = photo_file_path
                            else:
                                logger.warning(f"{log_prefix}: Photo path '{photo_file_path}' not found on disk. Sending without photo. Please re-upload the photo in settings.")

                        if photo_to_send:
                            logger.info(f"{log_prefix}: -> {target_log_name} {log_msg_count} с ФОТО")
                            await client.send_photo(
                                chat_id=target_user.id,
                                photo=photo_to_send,
                                caption=text_to_send
                            )
                        else:
                            logger.info(f"{log_prefix}: -> {target_log_name} {log_msg_count}")
                            await client.send_message(chat_id=target_user.id, text=text_to_send)
                        async with stats_lock:
                            if user_id in ATTACK_STATS:
                                ATTACK_STATS[user_id]["messages"] += 1
                        await asyncio.sleep(attack_delay)

                    except PeerIdInvalid:
                        reason = "Невалидный ID цели"
                        logger.warning(f"{log_prefix}: Не могу написать {target_log_name} из-за {reason} (PeerIdInvalid). Пропускаю цель.")
                        async with stats_lock:
                            if user_id in ATTACK_STATS:
                                stats = ATTACK_STATS[user_id]
                                stats["errors"] += 1
                                stats["error_details"].append(f"Невалидный ID: {target_log_name}")
                        break  # Выходим из for, переходим к finally

                    except (PeerFlood, FloodWait) as e:
                        # --- ИЗМЕНЕНО: Устанавливаем глобальный кулдаун для всей задачи ---
                        # Воркер больше не завершается, а уходит на перерыв вместе со всеми.
                        wait_time = e.value if isinstance(e, FloodWait) else 300
                        reason = f"PeerFlood/FloodWait ({wait_time} сек)"
                        logger.warning(f"{log_prefix}: {reason}. Вся атака для пользователя {user_id} будет приостановлена.")

                        cooldown_until = time.time() + wait_time + 5
                        async with ATTACK_COOLDOWN_LOCK:
                            ATTACK_COOLDOWN_UNTIL[user_id] = cooldown_until

                        async with SESSION_MUTE_LOCK:
                            SESSION_MUTE_UNTIL[session_name] = cooldown_until

                        logger.info(f"{log_prefix}: Возвращаю цель {target_log_name} в очередь и ухожу на перерыв.")
                        await target_queue.put(target_identifier)
                        continue # Переходим к следующей итерации, где сработает проверка кулдауна

                    except UserPrivacyRestricted:
                        reason = "Настройки приватности цели"
                        logger.warning(f"{log_prefix}: Не могу написать {target_log_name} из-за {reason}. Пропускаю цель.")
                        async with stats_lock:
                            if user_id in ATTACK_STATS:
                                stats = ATTACK_STATS[user_id]
                                stats["errors"] += 1
                                stats["error_details"].append(f"Приватность: {target_log_name}")
                        break # Выходим из for, переходим к finally

                    except Exception as e:
                        error_type_name = type(e).__name__
                        logger.error(f"{log_prefix}: Ошибка отправки {target_log_name}: {e}", exc_info=True)
                        async with stats_lock:
                            if user_id in ATTACK_STATS:
                                stats = ATTACK_STATS[user_id]
                                stats["errors"] += 1
                                stats["error_details"].append(f"Ошибка отправки ({target_log_name}): {error_type_name}")
                        await asyncio.sleep(3)

            finally:
                target_queue.task_done()
                logger.debug(f"{log_prefix}: task_done() вызван для цели {target_identifier}")

    except asyncio.CancelledError:
        logger.info(f"{log_prefix}: Получен сигнал отмены.")
    except (AuthKeyUnregistered, UserDeactivated) as e:
        error_name = type(e).__name__
        logger.error(f"{log_prefix} неработоспособен ({error_name}). Удаляю сессию.")
        await db_manager.delete_session(user_id, phone_for_log)
        await record_worker_session_failure(
            user_id, phone_for_log, f"{error_name} (удалена)", stats_lock, ATTACK_STATS, log_prefix, bot=bot
        )
    except Exception as e:
        logger.critical(f"Критическая ошибка в worker'е {phone_for_log} (атака в ЛС): {e}", exc_info=True)
        await record_worker_session_failure(
            user_id, phone_for_log, f"Критическая ошибка: {type(e).__name__}", stats_lock, ATTACK_STATS, log_prefix, bot=bot
        )
    finally:
        if client and client.is_connected:
            await client.disconnect()
        logger.info(f"Worker (атака в ЛС) для сессии {phone_for_log} завершил работу.")


async def attack_loop_task(
    user_id: int, bot: Bot, attack_mode: str,
    target_nickname: Optional[str], message_count: int,
    attack_delay: float, use_ai: bool, is_infinite: bool,
    session_limit: Optional[int] 
):
    # Local import to break a likely circular dependency
    from bot.client_tasks.client_manager import (
        RESERVED_SESSIONS, RESERVED_SESSIONS_LOCK
    )
    """Основная задача для запуска атаки в ЛС."""
    log_prefix = f"ATTACK_LOOP [{user_id}]"
    logger.info(f"{log_prefix}: Начало цикла АТАКИ В ЛС. Режим: {attack_mode}.")
    active_sessions = {} # Определяем здесь для доступа в finally
    
    target_queue = None  # Инициализируем здесь для безопасности в блоке finally
    workers = []
    # --- ИЗМЕНЕНО: Инициализация Lock'а до блока try для безопасности в finally ---
    stats_lock = asyncio.Lock()

    try:
        user_config = await db_manager.get_user_data(user_id)
        all_user_sessions = user_config['sessions']
        comment_texts = await db_manager.get_comments(user_id)
        photo_file_path = await db_manager.get_spam_photo(user_id)
        proxies_list_str = user_config['proxies']
        ai_settings = await db_manager.get_ai_settings(user_id)
        stop_event = ATTACK_STOP_EVENTS.get(user_id)

        # 3. Подготовка сессий и прокси (перенесено выше для использования в парсинге)
        proxies = []
        if ai_settings.get("use_proxy", True):
            proxies = [parse_proxy_string(p) for p in proxies_list_str]
            proxies = [p for p in proxies if p]
            logger.info(f"{log_prefix}: Найдено {len(proxies)} валидных прокси для использования.")

        # 1. Подготовка очереди целей
        target_queue = asyncio.Queue()
        total_targets = 0
        if attack_mode == 'mass':
            async for target_id in db_manager.get_scraped_users_stream(user_id):
                await target_queue.put(target_id)
                total_targets += 1
            if total_targets == 0:
                is_spam = SPAM_STATUS.get(user_id, False)
                markup = tasks_keyboard(is_spam_active=is_spam, is_attack_active=False)
                await bot.send_message(user_id, "❌ Не удалось запустить массовую атаку: база собранных пользователей пуста.", reply_markup=markup)
                ATTACK_STATUS[user_id] = False
                ATTACK_STOP_EVENTS.pop(user_id, None)
                return
        else:  # single mode, can be a user or a group
            client = None
            try:
                if not all_user_sessions:
                    raise ValueError("Нет доступных сессий для определения цели.")

                phone, session_file_path = random.choice(list(all_user_sessions.items()))
                s_name = os.path.splitext(os.path.basename(session_file_path))[0]
                
                proxy = None
                if proxies: # Теперь 'proxies' определен
                    proxy = random.choice(proxies)

                client = await get_connected_client(user_id, s_name, no_updates=True, proxy=proxy)
                if not client:
                    raise ConnectionError(f"Не удалось подключиться к сессии {phone} для определения цели.")

                logger.info(f"{log_prefix}: Определяю тип цели '{target_nickname}' с помощью сессии {phone}.")
                target_chat = await client.get_chat(target_nickname)

                if target_chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]:
                    logger.info(f"{log_prefix}: Цель - группа ({target_nickname}). Вступаю в группу и начинаю сбор участников...")

                    # --- ИЗМЕНЕНО: Сессия будет пытаться вступить в чат для сбора участников ---
                    try:
                        await client.join_chat(target_nickname)
                        logger.info(f"{log_prefix}: Успешно вступил в группу {target_nickname}.")
                    except UserAlreadyParticipant:
                        logger.info(f"{log_prefix}: Сессия уже является участником {target_nickname}.")
                        pass  # Все в порядке, продолжаем

                    skip_admins = ai_settings.get("attack_skip_admins", True)
                    
                    async for member in client.get_chat_members(target_chat.id):
                        user = member.user
                        if user.is_bot or user.is_deleted:
                            continue
                        
                        if skip_admins and member.status in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER]:
                            logger.debug(f"{log_prefix}: Пропускаю админа/владельца {user.id}")
                            continue
                        
                        await target_queue.put(user.id)
                        total_targets += 1
                    
                    if total_targets == 0:
                        raise ValueError("В указанной группе не найдено подходящих участников для атаки.")
                    
                    # Обновляем имя в статистике для более понятного отчета
                    target_nickname = target_chat.title or target_nickname

                else:  # Предполагаем, что это пользователь
                    await target_queue.put(target_nickname)
                    total_targets = 1

            except (UsernameNotOccupied, ChannelPrivate, ValueError) as e:
                error_text = f"❌ Не удалось запустить атаку: не найдена цель (юзер или группа) '{target_nickname}'. Ошибка: {e}"
                logger.warning(f"{log_prefix}: {error_text}")
                is_spam = SPAM_STATUS.get(user_id, False)
                markup = tasks_keyboard(is_spam_active=is_spam, is_attack_active=False)
                await bot.send_message(user_id, error_text, reply_markup=markup)
                ATTACK_STATUS[user_id] = False
                ATTACK_STOP_EVENTS.pop(user_id, None)
                return
            finally:
                if client and client.is_connected:
                    await client.disconnect()

        # 2. Инициализация статистики
        total_messages_to_send = "∞" if is_infinite else message_count * total_targets
        ATTACK_STATS[user_id] = {
            "messages": 0, "errors": 0, "nickname": target_nickname,
            "total_sessions": 0,
            "total_messages": total_messages_to_send, "delay": attack_delay,
            "total_targets": total_targets,
            "failed_sessions": [],
            "error_details": []
        }

        # --- ИЗМЕНЕНО: Логика резервирования сессий ---
        async with RESERVED_SESSIONS_LOCK:
            if user_id not in RESERVED_SESSIONS:
                RESERVED_SESSIONS[user_id] = {}
            
            reserved_for_user = RESERVED_SESSIONS.get(user_id, {})
            
            eligible_sessions = {}
            for phone, session_file_path in all_user_sessions.items():
                s_name = os.path.splitext(os.path.basename(session_file_path))[0]
                
                # Check mute (from FloodWait)
                if time.time() < SESSION_MUTE_UNTIL.get(s_name, 0):
                    logger.warning(f"{log_prefix}: Сессия {s_name} в муте. Пропускается.")
                    continue
                
                # Check reservation
                if s_name in reserved_for_user:
                    logger.info(f"{log_prefix}: Сессия {s_name} зарезервирована для '{reserved_for_user[s_name]}'. Пропускается.")
                    continue
                
                eligible_sessions[phone] = session_file_path

            session_items = list(eligible_sessions.items())
            random.shuffle(session_items)

            if session_limit is not None and session_limit > 0:
                session_items = session_items[:session_limit]

            active_sessions = dict(session_items)
            for phone, session_file_path in active_sessions.items():
                s_name = os.path.splitext(os.path.basename(session_file_path))[0]
                RESERVED_SESSIONS[user_id][s_name] = 'attack'
            logger.info(f"{log_prefix}: Зарезервировано {len(active_sessions)} сессий для атаки.")

        num_sessions = len(active_sessions)
        if num_sessions == 0:
            is_spam = SPAM_STATUS.get(user_id, False)
            markup = tasks_keyboard(is_spam_active=is_spam, is_attack_active=False)
            await bot.send_message(user_id, "❌ Не удалось запустить атаку: нет доступных (незанятых) сессий.", reply_markup=markup)
            ATTACK_STATUS[user_id] = False
            ATTACK_STOP_EVENTS.pop(user_id, None)
            return

        # 4. Создание воркеров
        proxy_cycle = itertools.cycle(proxies) if proxies else None

        ATTACK_STATS[user_id]["total_sessions"] = num_sessions
        for phone, session_file_path in active_sessions.items():
            assigned_proxy = next(proxy_cycle) if proxy_cycle else None
            s_name = os.path.splitext(os.path.basename(session_file_path))[0]
            worker = asyncio.create_task(_attack_worker(
                bot=bot, user_id=user_id, session_name=s_name, phone_for_log=phone,
                target_queue=target_queue, message_count=message_count,
                attack_delay=attack_delay, use_ai=use_ai, comment_texts=comment_texts,
                ai_settings=ai_settings, stop_event=stop_event, stats_lock=stats_lock,
                is_infinite=is_infinite, photo_file_path=photo_file_path, proxy=assigned_proxy
            ))
            workers.append(worker)

        # 5. Ожидание завершения
        queue_waiter_task = asyncio.create_task(target_queue.join())
        stop_waiter_task = asyncio.create_task(stop_event.wait())

        done, pending = await asyncio.wait(
            {queue_waiter_task, stop_waiter_task},
            return_when=asyncio.FIRST_COMPLETED
        )

        if stop_waiter_task in done:
            logger.info(f"{log_prefix}: Получен сигнал остановки. Завершаем работу...")
        elif queue_waiter_task in done:
            logger.info(f"{log_prefix}: Все цели обработаны. Завершаем работу...")

    except Exception as e:
        logger.critical(f"Критическая ошибка в attack_loop_task {user_id}: {e}", exc_info=True)
    finally:
        logger.info(f"{log_prefix}: Завершение цикла атаки в ЛС.")

        # --- ИЗМЕНЕНО: Очищаем глобальный кулдаун при завершении задачи ---
        async with ATTACK_COOLDOWN_LOCK:
            ATTACK_COOLDOWN_UNTIL.pop(user_id, None)

        # --- ИЗМЕНЕНО: Освобождение зарезервированных сессий ---
        if active_sessions:
            async with RESERVED_SESSIONS_LOCK:
                if user_id in RESERVED_SESSIONS:
                    released_count = 0
                    for session_file_path in active_sessions.values():
                        s_name = os.path.splitext(os.path.basename(session_file_path))[0]
                        if RESERVED_SESSIONS[user_id].pop(s_name, None):
                            released_count += 1
                    logger.info(f"{log_prefix}: Освобождено {released_count} сессий из-под задачи 'атака'.")

        # Отменяем все еще работающие задачи
        for task in workers:
            task.cancel()
        # Также отменяем ожидающие задачи (если они есть)
        if 'pending' in locals():
            for task in pending:
                task.cancel()
        # Собираем результаты отмененных задач
        await asyncio.gather(*workers, return_exceptions=True)
        if 'pending' in locals():
            await asyncio.gather(*pending, return_exceptions=True)

        async with ATTACK_STATUS_LOCK:
            ATTACK_STATUS[user_id] = False
        ATTACK_STOP_EVENTS.pop(user_id, None)

        async with stats_lock:
            final_stats = ATTACK_STATS.pop(user_id, {})

        safe_nick = html.escape(final_stats.get('nickname', 'N/A'))

        report_message = f"<b>🏁 Атака в ЛС на <code>{safe_nick}</code> завершена.</b>\n\n"
        report_message += f"<b>📈 Статистика:</b>\n"
        report_message += f"  - Использовано сессий: {final_stats.get('total_sessions', '?')}\n"
        total_msgs_text = final_stats.get('total_messages', '?')
        report_message += f"  - Отправлено: {final_stats.get('messages', 0)} / {total_msgs_text}\n"
        if attack_mode == 'mass' and target_queue is not None:
            processed_targets = final_stats.get('total_targets', 0) - target_queue.qsize()
            report_message += f"  - Обработано целей: {processed_targets} / {final_stats.get('total_targets', '?')}\n"
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

        is_spam = SPAM_STATUS.get(user_id, False)
        markup = tasks_keyboard(is_spam_active=is_spam, is_attack_active=False)
        # --- ИЗМЕНЕНО: Добавлена обработка ошибок при отправке финального отчета ---
        try:
            await bot.send_message(user_id, report_message, reply_markup=markup)
        except Exception as e:
            logger.error(f"{log_prefix}: Не удалось отправить финальный отчет пользователю {user_id}: {e}")