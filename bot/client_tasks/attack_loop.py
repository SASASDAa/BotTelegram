# bot/client_tasks/attack_loop.py
import asyncio
import html
import itertools
import logging
import os
import random
from collections import Counter
from typing import Optional, Tuple

from aiogram import Bot
from telethon import functions
from telethon.errors.rpcerrorlist import (
    FloodWaitError, PeerFloodError, UserPrivacyRestrictedError,
    UserNotParticipantError, UserBannedInChannelError, ChannelPrivateError,
    UsernameNotOccupiedError, UserDeactivatedError, AuthKeyUnregisteredError
)
from telethon.tl.types import ChannelParticipantsAdmins, User

from bot.client_tasks.client_manager import (
    ATTACK_COOLDOWN_LOCK, ATTACK_COOLDOWN_UNTIL, ATTACK_STATS, ATTACK_STATUS,
    ATTACK_STATUS_LOCK, ATTACK_STOP_EVENTS, SPAM_STATUS, get_connected_telethon_client
)
from bot.client_tasks.task_utils import is_user_active
from bot.database.db_manager import db_manager
from bot.client_tasks.task_utils import (
    get_unique_text_with_fallback, record_worker_session_failure
)
from bot.utils.proxy_parser import parse_proxy_string
from bot.keyboards import tasks_keyboard

logger = logging.getLogger(__name__)


async def _attack_worker(
        bot: Bot, user_id: int, session_name: str, phone_for_log: str,
        target_queue: asyncio.Queue[dict | int | str], message_count: int, attack_delay: float,
        use_ai: bool, comment_texts: list, ai_settings: dict,
        stop_event: asyncio.Event, stats_lock: asyncio.Lock, is_infinite: bool,
        photo_file_path: Optional[str] = None, proxy: Optional[dict] = None,
        target_group_username: Optional[str] = None
):
    """Worker task for a single Telethon session to send DMs."""
    logger.info(f"Telethon Worker (атака в ЛС) для сессии {phone_for_log} запущен.")

    client = None
    log_prefix = f"Telethon Worker {phone_for_log} (атака в ЛС)"
    try:
        client = await get_connected_telethon_client(user_id, session_name, proxy=proxy)
        if not client:
            await record_worker_session_failure(
                user_id, phone_for_log, "Не удалось подключиться", stats_lock,
                ATTACK_STATS, log_prefix, bot=bot
            )
            return

        # Join the target group and pre-cache members to ensure peer visibility
        if target_group_username:
            try:
                logger.info(f"{log_prefix}: Вступаю в целевую группу {target_group_username} для обеспечения доставки сообщений.")
                group_entity = await client.get_entity(target_group_username)
                await client(functions.channels.JoinChannelRequest(channel=group_entity))

                # --- НОВАЯ ЛОГИКА: Предварительное кэширование участников ---
                # Это действие заполняет кэш сессии, позволяя client.get_entity(id)
                # работать локально без сетевых запросов, что решает проблему ValueError: Peer not found
                # и снижает количество запросов ResolveUsernameRequest, предотвращая FloodWait.
                logger.info(f"{log_prefix}: Начинаю пред-кэширование участников из {target_group_username}...")
                async for _ in client.iter_participants(group_entity):
                    pass # Просто итерируем, чтобы заполнить кэш
                logger.info(f"{log_prefix}: Пред-кэширование участников завершено.")
            except Exception as e:
                # Not fatal, but might cause issues. Log a warning and continue.
                logger.warning(
                    f"{log_prefix}: Не удалось вступить в группу или кэшировать участников {target_group_username}: {e}. Атака может быть нестабильной.")

        while not stop_event.is_set():
            try:
                target_obj = await target_queue.get()
            except asyncio.CancelledError:
                break

            target_id_for_log = "N/A"
            try:
                # --- ИЗМЕНЕНО: Новая стратегия разрешения цели ---
                # 1. Сначала пытаемся найти цель по ID. После пред-кэширования это должно быть быстро и локально.
                # 2. Если по ID не найдено (юзер мог покинуть группу), пробуем найти по юзернейму (если он есть).
                # Это значительно сокращает количество сетевых запросов и предотвращает FloodWait.
                target_peer = None
                if isinstance(target_obj, dict):  # Это {'id': ..., 'username': ...} из сбора
                    target_id = target_obj['id']
                    target_username = target_obj.get('username')
                    target_id_for_log = str(target_id)

                    try:
                        logger.debug(f"{log_prefix}: Пытаюсь разрешить цель по ID {target_id_for_log} (из кэша).")
                        target_peer = await client.get_entity(target_id)
                    except (ValueError, TypeError):
                        if target_username:
                            logger.warning(f"{log_prefix}: Не удалось найти цель по ID {target_id_for_log}. Пробую по юзернейму @{target_username}.")
                            target_peer = await client.get_entity(target_username)
                        else:
                            # Если юзернейма нет, то найти цель уже не получится.
                            raise ValueError("Цель не найдена по ID и юзернейм отсутствует.")

                else:  # Это ID из базы (для mass-mode)
                    target_id_for_log = str(target_obj)
                    target_peer = await client.get_entity(target_obj)
                
                # --- ИСПРАВЛЕНО: Цикл отправки сообщений перенесен в блок try ---
                # Ранее этот код был ошибочно размещен в блоке except после `continue`,
                # из-за чего он никогда не выполнялся.
                message_iterator = itertools.count(1) if is_infinite else range(1, message_count + 1)

                for i in message_iterator:
                    if stop_event.is_set():
                        logger.info(f"{log_prefix}: Получен сигнал остановки. Возвращаю цель {target_id_for_log} в очередь.")
                        await target_queue.put(target_obj)
                        break

                    original_comment_text = random.choice(comment_texts)
                    text_to_send = original_comment_text
                    if use_ai and ai_settings.get("enabled") and ai_settings.get("api_key"):
                        text_to_send = await get_unique_text_with_fallback(
                            original_text=original_comment_text, user_id=user_id,
                            ai_settings=ai_settings, stats_lock=stats_lock, stats_dict=ATTACK_STATS,
                            log_prefix=f"{log_prefix} -> {target_id_for_log}"
                        )

                    try:
                        log_msg_count = f"(#{i})" if is_infinite else f"({i}/{message_count})"

                        photo_to_send = None
                        if photo_file_path:
                            if os.path.exists(photo_file_path):
                                photo_to_send = photo_file_path
                            else:
                                logger.warning(
                                    f"{log_prefix}: Photo path '{photo_file_path}' not found. Sending without photo.")

                        if photo_to_send:
                            logger.info(f"{log_prefix}: -> {target_id_for_log} {log_msg_count} с ФОТО")
                            await client.send_file(target_peer, file=photo_to_send, caption=text_to_send)
                        else:
                            logger.info(f"{log_prefix}: -> {target_id_for_log} {log_msg_count}")
                            await client.send_message(target_peer, text_to_send)

                        async with stats_lock:
                            if user_id in ATTACK_STATS:
                                ATTACK_STATS[user_id]["messages"] += 1
                        await asyncio.sleep(attack_delay)

                    except (PeerFloodError, FloodWaitError) as e:
                        wait_time = e.seconds if isinstance(e, FloodWaitError) else 300
                        reason = f"PeerFlood/FloodWaitError ({wait_time} сек)"
                        logger.warning(f"{log_prefix}: {reason}. Вся атака для пользователя {user_id} будет приостановлена.")

                        cooldown_until = asyncio.get_event_loop().time() + wait_time + 5
                        async with ATTACK_COOLDOWN_LOCK:
                            # --- ИЗМЕНЕНО: Отправка уведомления пользователю о FloodWait ---
                            # Проверяем, не было ли уже отправлено уведомление для этого периода,
                            # чтобы избежать спама, если несколько воркеров одновременно получат ошибку.
                            old_cooldown = ATTACK_COOLDOWN_UNTIL.get(user_id, 0)
                            ATTACK_COOLDOWN_UNTIL[user_id] = cooldown_until

                            if cooldown_until > old_cooldown + 10:  # Отправляем, только если новый кулдаун значительно позже
                                try:
                                    from bot.keyboards import attack_flood_wait_keyboard  # Локальный импорт
                                    wait_minutes = round(wait_time / 60)
                                    notification_text = (
                                        f"⚠️ <b>Атака приостановлена из-за Flood-ограничений</b>\n\n"
                                        f"Одна из ваших сессий (<code>{html.escape(phone_for_log)}</code>) столкнулась с временными ограничениями от Telegram. "
                                        f"Чтобы избежать блокировки аккаунтов, все сессии в этой задаче уходят на перерыв примерно на <b>{wait_minutes} минут</b>."
                                    )
                                    await bot.send_message(user_id, notification_text, reply_markup=attack_flood_wait_keyboard())
                                except Exception as notify_error:
                                    logger.error(f"{log_prefix}: Не удалось отправить уведомление о FloodWait пользователю {user_id}: {notify_error}")

                        logger.info(f"{log_prefix}: Возвращаю цель {target_id_for_log} в очередь и ухожу на перерыв.")
                        await target_queue.put(target_obj)
                        await asyncio.sleep(wait_time + 5)
                        continue

                    except UserPrivacyRestrictedError:
                        reason = "Настройки приватности цели"
                        logger.warning(f"{log_prefix}: Не могу написать {target_id_for_log} из-за {reason}. Пропускаю цель.")
                        async with stats_lock:
                            if user_id in ATTACK_STATS:
                                stats = ATTACK_STATS[user_id]
                                stats["errors"] += 1
                                stats["error_details"].append(f"Приватность: {target_id_for_log}")
                        break  # Exit the for loop, move to the next target

                    except Exception as e:
                        error_type_name = type(e).__name__
                        logger.error(f"{log_prefix}: Ошибка отправки {target_id_for_log}: {e}", exc_info=True)
                        async with stats_lock:
                            if user_id in ATTACK_STATS:
                                stats = ATTACK_STATS[user_id]
                                stats["errors"] += 1
                                stats["error_details"].append(f"Ошибка отправки ({target_id_for_log}): {error_type_name}")
                        await asyncio.sleep(3)

            except Exception as e:
                reason = f"Не удалось разрешить цель {target_id_for_log} ({type(e).__name__})"
                logger.warning(f"{log_prefix}: {reason}. Пропускаю цель.")
                async with stats_lock:
                    if user_id in ATTACK_STATS:
                        stats = ATTACK_STATS[user_id]
                        stats["errors"] += 1
                        stats["error_details"].append(f"Не удалось разрешить цель: {target_id_for_log}")
                continue  # Skip this target and get the next one

            finally:
                target_queue.task_done()
                logger.debug(f"{log_prefix}: task_done() вызван для цели {target_id_for_log}")

    except asyncio.CancelledError:
        logger.info(f"{log_prefix}: Получен сигнал отмены.")
    except (AuthKeyUnregisteredError, UserDeactivatedError) as e:
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
        if client and client.is_connected() and target_group_username:
            try:
                logger.info(f"{log_prefix}: Покидаю целевую группу {target_group_username}.")
                await client(functions.channels.LeaveChannelRequest(channel=target_group_username))
            except Exception as e:
                logger.warning(f"{log_prefix}: Не удалось покинуть группу {target_group_username}: {e}")

        if client and client.is_connected():
            await client.disconnect()
        logger.info(f"Telethon Worker (атака в ЛС) для сессии {phone_for_log} завершил работу.")


async def attack_loop_task(
    user_id: int, bot: Bot, attack_mode: str,
    target_nickname: Optional[str], message_count: int,
    attack_delay: float, use_ai: bool, is_infinite: bool,
    session_limit: Optional[int] 
): # pragma: no cover
    """Основная задача для запуска атаки в ЛС с использованием Telethon."""
    # --- ИЗМЕНЕНО: Переменная для хранения итогового имени цели для отчета ---
    # Инициализируем ее сразу, чтобы избежать N/A в отчете, если что-то пойдет не так.
    resolved_target_name = target_nickname
    if attack_mode == 'mass':
        resolved_target_name = "массовая атака по базе"

    log_prefix = f"ATTACK_LOOP [{user_id}]"
    logger.info(f"{log_prefix}: Начало цикла АТАКИ В ЛС. Режим: {attack_mode}.")
    active_sessions = {}
    
    target_queue = None  # Инициализируем здесь для безопасности в блоке finally
    workers = []
    pending = set()
    # --- ИЗМЕНЕНО: Инициализация Lock'а до блока try для безопасности в finally ---
    stats_lock = asyncio.Lock()

    try:
        user_config = await db_manager.get_user_data(user_id)
        all_telethon_sessions = await db_manager.get_sessions_by_type(user_id, 'telethon')
        comment_texts = await db_manager.get_comments(user_id)
        photo_file_path = await db_manager.get_spam_photo(user_id)
        proxies_list_str = user_config['proxies']
        ai_settings = await db_manager.get_ai_settings(user_id)
        activity_filter = ai_settings.get("user_activity_filter", "all")
        stop_event = ATTACK_STOP_EVENTS.get(user_id)

        proxies = []
        if ai_settings.get("use_proxy", True):
            proxies = [parse_proxy_string(p) for p in proxies_list_str]
            proxies = [p for p in proxies if p]
            logger.info(f"{log_prefix}: Найдено {len(proxies)} валидных прокси для использования.")

        # 1. Подготовка очереди целей. В очередь кладем ID или username.
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
            target_group_for_workers = None
            client = None
            # --- ИЗМЕНЕНО: Инициализация переменных для использования в блоке finally ---
            is_group_target = False
            target_entity = None
            phone = None
            # Для определения цели и сбора участников используем одну из сессий
            try:
                if not all_telethon_sessions:
                    raise ValueError("Нет доступных Telethon сессий для определения цели.")

                phone, session_file_path = random.choice(list(all_telethon_sessions.items()))
                s_name = os.path.splitext(os.path.basename(session_file_path))[0]
                
                proxy = None
                if proxies:
                    proxy = random.choice(proxies)

                client = await get_connected_telethon_client(user_id, s_name, proxy=proxy)
                if not client:
                    raise ConnectionError(f"Не удалось подключиться к сессии {phone} для определения цели.")

                logger.info(f"{log_prefix}: Определяю тип цели '{target_nickname}' с помощью сессии {phone}.")
                target_entity = await client.get_entity(target_nickname)

                # --- ИЗМЕНЕНО: Определяем имя цели сразу после получения entity ---
                # Это гарантирует, что в отчете будет правильное имя, даже если сбор участников прервется.
                if hasattr(target_entity, 'title') and target_entity.title:
                    resolved_target_name = target_entity.title

                is_group_target = hasattr(target_entity, 'broadcast') and not target_entity.broadcast

                if is_group_target:
                    logger.info(f"{log_prefix}: Цель - группа ({resolved_target_name}). Вступаю в группу и начинаю сбор участников...")
                    target_group_for_workers = target_nickname
                    
                    try:
                        await client(functions.channels.JoinChannelRequest(channel=target_entity))
                        logger.info(f"{log_prefix}: Успешно вступил в группу {target_nickname}.")
                    except UserBannedInChannelError:
                        raise ValueError(f"Сессия {phone} забанена в группе {target_nickname}.")
                    except Exception:
                        logger.info(f"{log_prefix}: Сессия уже является участником {target_nickname}.")

                    # --- ИЗМЕНЕНО: Оптимизированный сбор участников ---
                    # Список админов запрашивается один раз до цикла, чтобы избежать FloodWait.
                    skip_admins = ai_settings.get("attack_skip_admins", True)
                    admin_ids = set()
                    if skip_admins:
                        logger.info(f"{log_prefix}: Получаю список администраторов для исключения...")
                        try:
                            # Использование iter_participants - эффективный способ получить всех админов.
                            # Параметр aggressive здесь не требуется.
                            async for admin in client.iter_participants(target_entity, filter=ChannelParticipantsAdmins):
                                admin_ids.add(admin.id)
                            logger.info(f"{log_prefix}: Найдено {len(admin_ids)} администраторов для исключения.")
                        except Exception as e:
                            logger.warning(f"{log_prefix}: Не удалось получить список админов: {e}. Сбор продолжится без их исключения.")
                    
                    # --- ИЗМЕНЕНО: Используем iter_participants для потокового сбора ---
                    # Это более эффективно по памяти для очень больших групп по сравнению
                    # с get_participants, который загружает всех сразу.
                    logger.info(f"{log_prefix}: Начинаю потоковый сбор и фильтрацию участников из {resolved_target_name}...")
                    async for user in client.iter_participants(target_entity):
                        # Проверяем все условия в одной строке, включая проверку на админа
                        if user.is_self or user.bot or user.deleted or (user.id in admin_ids) or not is_user_active(user.status, activity_filter):
                            continue

                        # --- ИЗМЕНЕНО: Кладем в очередь словарь с ID и username, а не весь объект ---
                        await target_queue.put({'id': user.id, 'username': user.username})
                        total_targets += 1
                    logger.info(f"{log_prefix}: Сбор и фильтрация завершены. Найдено {total_targets} целей.")

                    if total_targets == 0:
                        raise ValueError("В указанной группе не найдено подходящих участников для атаки.")
                    # --- ИЗМЕНЕНО: Эта строка больше не нужна, имя уже определено выше ---
                    # target_nickname = target_entity.title or target_nickname
                else:  # Это пользователь
                    if hasattr(target_entity, 'first_name'):
                        resolved_target_name = target_entity.first_name
                        if hasattr(target_entity, 'last_name') and target_entity.last_name:
                            resolved_target_name += f" {target_entity.last_name}"
                    # --- ИЗМЕНЕНО: Кладем в очередь словарь с ID и username ---
                    await target_queue.put({'id': target_entity.id, 'username': getattr(target_entity, 'username', None)})
                    total_targets = 1
            except (UsernameNotOccupiedError, ChannelPrivateError, ValueError) as e:
                error_text = f"❌ Не удалось запустить атаку: не найдена цель (юзер или группа) '{target_nickname}'. Ошибка: {e}"
                logger.warning(f"{log_prefix}: {error_text}")
                is_spam = SPAM_STATUS.get(user_id, False)
                markup = tasks_keyboard(is_spam_active=is_spam, is_attack_active=False)
                await bot.send_message(user_id, error_text, reply_markup=markup)
                ATTACK_STATUS[user_id] = False
                ATTACK_STOP_EVENTS.pop(user_id, None)
                return
            finally:
                if client and client.is_connected():
                    # --- ДОБАВЛЕНО: Покидаем группу после сбора участников ---
                    if is_group_target and target_entity:
                        try:
                            logger.info(f"{log_prefix}: Сессия {phone} покидает группу {resolved_target_name} после сбора участников.")
                            await client(functions.channels.LeaveChannelRequest(channel=target_entity))
                        except Exception as e:
                            logger.warning(f"{log_prefix}: Не удалось покинуть группу {resolved_target_name} сессией {phone}: {e}")
                    await client.disconnect()

        # 2. Инициализация статистики
        total_messages_to_send = "∞" if is_infinite else message_count * total_targets
        ATTACK_STATS[user_id] = {
            "messages": 0, "errors": 0, "nickname": resolved_target_name,
            "total_sessions": 0,
            "total_messages": total_messages_to_send, "delay": attack_delay,
            "total_targets": total_targets,
            "failed_sessions": [],
            "error_details": []
        }

        # Local import to break a likely circular dependency
        from bot.client_tasks.client_manager import (
            RESERVED_SESSIONS, RESERVED_SESSIONS_LOCK
        )
        # --- ИЗМЕНЕНО: Логика выбора сессий ---
        # Используем только Telethon сессии
        session_items = list(all_telethon_sessions.items())
        random.shuffle(session_items)

        if session_limit is not None and session_limit > 0:
            session_items = session_items[:session_limit]

        active_sessions = dict(session_items)
        async with RESERVED_SESSIONS_LOCK:
            if user_id not in RESERVED_SESSIONS: RESERVED_SESSIONS[user_id] = {}
            for phone, session_file_path in active_sessions.items():
                s_name = os.path.splitext(os.path.basename(session_file_path))[0]
                RESERVED_SESSIONS[user_id][s_name] = 'attack'
        logger.info(f"{log_prefix}: Зарезервировано {len(active_sessions)} Telethon сессий для атаки.")

        num_sessions = len(active_sessions)
        if num_sessions == 0:
            is_spam = SPAM_STATUS.get(user_id, False)
            markup = tasks_keyboard(is_spam_active=is_spam, is_attack_active=False)
            await bot.send_message(user_id, "❌ Не удалось запустить атаку: нет доступных Telethon сессий.", reply_markup=markup)
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
                is_infinite=is_infinite, photo_file_path=photo_file_path, proxy=assigned_proxy,
                target_group_username=target_group_for_workers
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

        async with ATTACK_COOLDOWN_LOCK:
            ATTACK_COOLDOWN_UNTIL.pop(user_id, None)

        if active_sessions:
            async with RESERVED_SESSIONS_LOCK:
                if user_id in RESERVED_SESSIONS:
                    released_count = 0
                    for session_file_path in active_sessions.values():
                        s_name = os.path.splitext(os.path.basename(session_file_path))[0]
                        if RESERVED_SESSIONS[user_id].pop(s_name, None):
                            released_count += 1
                    logger.info(f"{log_prefix}: Освобождено {released_count} сессий из-под задачи 'атака'.")

        for task in workers:
            task.cancel()
        for task in pending:
            task.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
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
        try:
            await bot.send_message(user_id, report_message, reply_markup=markup)
        except Exception as e:
            logger.error(f"{log_prefix}: Не удалось отправить финальный отчет пользователю {user_id}: {e}")