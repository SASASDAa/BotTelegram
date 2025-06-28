# bot/client_tasks/warmer_loop.py
import asyncio
import html
import logging
import os
import random
import time
from collections import Counter, deque
from datetime import datetime, timedelta

from aiogram import Bot
from pyrogram.enums import ChatType, UserStatus
from pyrogram.errors import (
    AuthKeyUnregistered, UserDeactivated, FloodWait, UserChannelsTooMuch,
    MsgIdInvalid, ReactionEmpty
)

from bot.client_tasks.client_manager import (
    ACTIVE_WARMER_TASKS, WARMER_STATS, WARMER_STATUS, WARMER_STATUS_LOCK,
    WARMER_STOP_EVENTS, get_connected_client, SESSION_MUTE_LOCK,
    SESSION_MUTE_UNTIL
)
from bot.client_tasks.task_utils import record_worker_session_failure
from bot.database.db_manager import db_manager
from bot.keyboards import warmer_menu_keyboard

logger = logging.getLogger(__name__)

class PartnerUnavailableError(Exception):
    """Custom exception for when a dialogue partner is no longer available."""
    pass


REACTION_EMOJIS = ["👍", "🔥", "❤️", "🥰", "👏", "😁", "🎉", "💯", "👌"]

async def _perform_join_action(client, target_channels: list[str], log_prefix: str) -> str:
    """Joins a channel from the provided list."""
    if not target_channels:
        raise ValueError("Список целевых каналов для вступления пуст.")

    target_channel_input = random.choice(target_channels)

    # --- ИЗМЕНЕНО: Нормализация идентификатора канала ---
    # Pyrogram ожидает @username, а не полную ссылку https://t.me/username
    target_channel = target_channel_input
    if target_channel.startswith(('http://t.me/', 'https://t.me/')):
        path_part = target_channel.split('t.me/')[1]
        # Обрабатываем только публичные ссылки, joinchat-ссылки оставляем как есть
        if not path_part.startswith(('joinchat', '+')):
            target_channel = '@' + path_part.split('/')[0]

    logger.info(f"{log_prefix}: Пытаюсь вступить в целевой канал '{target_channel_input}' (нормализовано в '{target_channel}')")

    await client.join_chat(target_channel)
    logger.info(f"{log_prefix}: Успешно вступил в {target_channel_input}")
    return f"Вступил в {target_channel_input}"

async def _perform_reaction_action(client, target_channels: list[str], log_prefix: str) -> str:
    """Finds a post in a random channel from the target list and reacts to it."""
    logger.info(f"{log_prefix}: Ищу пост для реакции в целевых каналах.")

    if not target_channels:
        raise ValueError("Не указаны целевые каналы для реакций.")

    # Перемешиваем целевые каналы, чтобы не всегда пробовать первый
    random.shuffle(target_channels)

    # Попробуем до 5 случайных каналов, прежде чем сдаться
    for channel_identifier in target_channels[:5]:
        try:
            # Получаем чат, чтобы убедиться, что сессия в нем состоит
            # Это также нужно для получения ID чата, если на входе @username
            chat = await client.get_chat(channel_identifier)

            messages = [m async for m in client.get_chat_history(chat.id, limit=20)]
            if not messages:
                logger.debug(f"{log_prefix}: В целевом канале {chat.title} нет постов, пробую следующий.")
                continue

            target_message = random.choice(messages)
            reaction = random.choice(REACTION_EMOJIS)

            await client.send_reaction(chat.id, target_message.id, reaction)
            logger.info(f"{log_prefix}: Поставил реакцию '{reaction}' в {chat.title}")
            return f"Реакция '{reaction}' в {chat.title}"

        except (MsgIdInvalid, ReactionEmpty):
            logger.debug(f"{log_prefix}: Не удалось поставить реакцию в {channel_identifier} (пост удален/реакции отключены). Пробую другой канал.")
            continue # Попробуем следующий канал
        except Exception as e:
            # Логируем ошибку для конкретного канала, но не прерываем весь процесс
            logger.warning(f"{log_prefix}: Ошибка при работе с целевым каналом {channel_identifier}: {e}. Пробую другой.")
            continue

    # Если цикл завершился, а мы ничего не вернули, значит, не удалось поставить реакцию
    raise ValueError("Не удалось найти подходящий пост для реакции в 5 случайных целевых каналах")

async def _perform_dialogue_action(client, partner_peer_id: int, dialogue_phrases: list[str], log_prefix: str) -> str:
    """Sends a message to a partner session."""
    if not dialogue_phrases:
        raise ValueError("Список фраз для диалога пуст.")
    
    phrase = random.choice(dialogue_phrases)
    logger.info(f"{log_prefix}: Отправляю сообщение партнеру ({partner_peer_id}): '{phrase[:30]}...'")
    await client.send_message(partner_peer_id, phrase)
    return f"Сообщение партнеру ({partner_peer_id})"

async def _warmer_worker(
    bot: Bot, user_id: int, session_name: str, phone_for_log: str,
    settings: dict, stop_event: asyncio.Event, stats_lock: asyncio.Lock,
    partner_peer_id: int | None
):
    """Worker task for a single session to perform warming actions."""
    log_prefix = f"WARMER_WORKER [{phone_for_log}]"
    logger.info(f"{log_prefix}: Запущен.")

    def reset_daily_actions():
        """Helper to reset daily action counters."""
        return {
            'join': settings.get('join_channels_per_day', 2),
            'react': settings.get('send_reactions_per_day', 5),
            'dialogue': settings.get('dialogues_per_day', 3)
        }

    client = None
    actions_per_day = reset_daily_actions()
    current_day = datetime.now().day
    target_channels = [ch.strip() for ch in settings.get('target_channels', '').split(',') if ch.strip()]
    dialogue_phrases = [p.strip() for p in settings.get('dialogue_phrases', '').split(',') if p.strip()]
    
    # Make partner_peer_id mutable within the worker
    current_partner_peer_id = partner_peer_id
    try:
        client = await get_connected_client(user_id, session_name, no_updates=True)
        if not client:
            await record_worker_session_failure(user_id, phone_for_log, "Не удалось подключиться", stats_lock, WARMER_STATS, log_prefix, bot)
            return

        # --- ИЗМЕНЕНО: Усиленная проверка и принудительное заполнение client.me ---
        # Это необходимо для стабильной работы и избежания ошибок 'NoneType'
        me = await client.get_me()
        if not me:
            logger.warning(f"{log_prefix}: Не удалось получить данные о себе (get_me() failed). Завершаю воркер.")
            await record_worker_session_failure(user_id, phone_for_log, "Не удалось получить данные о себе", stats_lock, WARMER_STATS, log_prefix, bot)
            return
        
        client.me = me

        while not stop_event.is_set():
            # 1. Проверка на рабочие часы
            now = datetime.now()
            if settings.get('active_hours_enabled'):
                start_h = settings.get('active_hours_start')
                end_h = settings.get('active_hours_end')
                is_active_time = False
                if start_h < end_h: # Дневной график (e.g., 9-22)
                    if start_h <= now.hour < end_h: is_active_time = True
                else: # Ночной график (e.g., 22-06)
                    if now.hour >= start_h or now.hour < end_h: is_active_time = True

                if not is_active_time:
                    if now.hour >= end_h and start_h < end_h: # После дневного графика
                        target_time = now.replace(hour=start_h, minute=0, second=0, microsecond=0) + timedelta(days=1)
                    else: # До начала дневного или во время "перерыва" ночного
                        target_time = now.replace(hour=start_h, minute=0, second=0, microsecond=0)
                        if target_time < now: # Если время старта сегодня уже прошло
                             target_time += timedelta(days=1)

                    sleep_duration = (target_time - now).total_seconds()
                    logger.info(f"{log_prefix}: Нерабочие часы. Засыпаю на {sleep_duration / 3600:.2f} часов до {start_h:02}:00.")
                    try:
                        await asyncio.wait_for(stop_event.wait(), timeout=sleep_duration)
                        break # Stop event was set during sleep
                    except asyncio.TimeoutError:
                        continue # Woke up naturally, continue loop

            # Проверяем, наступил ли новый день, и сбрасываем счетчики
            if now.day != current_day:
                logger.info(f"{log_prefix}: Наступил новый день. Сбрасываю дневные лимиты действий.")
                actions_per_day = reset_daily_actions()
                current_day = now.day

            # 2. Выбор доступного действия
            possible_actions = []
            if actions_per_day['join'] > 0 and target_channels: possible_actions.append('join')
            if actions_per_day['react'] > 0 and target_channels: possible_actions.append('react')
            if actions_per_day['dialogue'] > 0 and current_partner_peer_id and dialogue_phrases:
                possible_actions.append('dialogue')

            if not possible_actions:
                logger.info(f"{log_prefix}: Дневные лимиты исчерпаны. Перехожу к следующей итерации сна.")
                # Fall through to the main sleep logic at the end of the loop
            else:
                action_type = random.choice(possible_actions)
            
                try:
                    if action_type == 'join':
                        action_result = await _perform_join_action(client, target_channels, log_prefix)
                        actions_per_day['join'] -= 1
                    elif action_type == 'react':
                        action_result = await _perform_reaction_action(client, target_channels, log_prefix)
                        actions_per_day['react'] -= 1
                    elif action_type == 'dialogue':
                        action_result = await _perform_dialogue_action(client, current_partner_peer_id, dialogue_phrases, log_prefix)
                        actions_per_day['dialogue'] -= 1

                    async with stats_lock:
                        if user_id in WARMER_STATS:
                            WARMER_STATS[user_id]["actions_done"] += 1
                            WARMER_STATS[user_id]["action_details"].append(action_result)

                    # Отправляем уведомление пользователю, если включено
                    if settings.get('inform_user_on_action'):
                        try:
                            notification_text = f"🔥 Прогрев: Сессия <code>{html.escape(phone_for_log)}</code> выполнила действие: {html.escape(action_result)}"
                            await bot.send_message(user_id, notification_text)
                        except Exception as e:
                            logger.warning(f"{log_prefix}: Не удалось отправить уведомление пользователю {user_id}: {e}")

                except PartnerUnavailableError:
                    logger.warning(f"{log_prefix}: Партнер для диалога недоступен. Отключаю диалоги для этой сессии.")
                    current_partner_peer_id = None # Disable for future loops
                    async with stats_lock:
                        if user_id in WARMER_STATS: WARMER_STATS[user_id]["errors"] += 1
    
                except FloodWait as e:
                    wait_time = e.value
                    logger.warning(f"{log_prefix}: FloodWait на {wait_time} сек. Сессия будет в муте.")
                    mute_until = time.time() + wait_time + 5
                    async with SESSION_MUTE_LOCK:
                        SESSION_MUTE_UNTIL[session_name] = mute_until
    
                    # Воркер прогрева просто ждет и продолжает, т.к. он долгоживущий
                    await asyncio.sleep(wait_time + 5)
                except (ValueError, UserChannelsTooMuch) as e:
                    logger.warning(f"{log_prefix}: Ошибка действия '{action_type}': {e}")
                    async with stats_lock:
                        if user_id in WARMER_STATS:
                            WARMER_STATS[user_id]["errors"] += 1
                except Exception as e:
                    logger.error(f"{log_prefix}: Критическая ошибка действия '{action_type}': {e}", exc_info=True)
                    async with stats_lock:
                        if user_id in WARMER_STATS:
                            WARMER_STATS[user_id]["errors"] += 1

            # 3. Sleep for a long random interval
            sleep_duration = random.uniform(3600 * 0.5, 3600 * 2) # 0.5 to 2 hours
            logger.info(f"{log_prefix}: Сплю до {sleep_duration / 3600:.2f} часов.")
            
            # Используем wait_for для мгновенной реакции на событие остановки
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=sleep_duration)
                break # Выходим из цикла, если stop_event был установлен
            except asyncio.TimeoutError:
                continue # Просыпаемся по таймауту и продолжаем работу

    except asyncio.CancelledError:
        logger.info(f"{log_prefix}: Получен сигнал отмены.")
        # This is a clean exit, the finally block will handle disconnection.
    except (AuthKeyUnregistered, UserDeactivated) as e:
        error_name = type(e).__name__
        await record_worker_session_failure(user_id, phone_for_log, f"{error_name} (удалена)", stats_lock, WARMER_STATS, log_prefix, bot)
        await db_manager.delete_session(user_id, phone_for_log)
    except Exception as e:
        logger.critical(f"{log_prefix}: Критическая ошибка: {e}", exc_info=True)
        await record_worker_session_failure(user_id, phone_for_log, f"Критическая ошибка: {type(e).__name__}", stats_lock, WARMER_STATS, log_prefix, bot)
    finally:
        if client and client.is_connected:
            await client.disconnect()
        logger.info(f"{log_prefix}: Завершил работу.")


async def warmer_loop_task(bot: Bot, user_id: int):
    """Main task to manage the warming process for a user."""
    log_prefix = f"WARMER_LOOP [{user_id}]"
    logger.info(f"{log_prefix}: Начало цикла прогрева.")

    workers = []
    # --- ИЗМЕНЕНО: Инициализация Lock'а до блока try для безопасности в finally ---
    stats_lock = asyncio.Lock()

    try:
        settings = await db_manager.get_warmer_settings(user_id)
        user_data = await db_manager.get_user_data(user_id)
        sessions = user_data.get('sessions', {})
        stop_event = WARMER_STOP_EVENTS.get(user_id)
        dialogue_phrases = [p.strip() for p in settings.get('dialogue_phrases', '').split(',') if p.strip()]

        WARMER_STATS[user_id] = {
            "actions_done": 0, "errors": 0, "failed_sessions": [],
            "action_details": [], "active_sessions": len(sessions)
        }

        # --- НОВАЯ ЛОГИКА: Спаривание сессий для диалогов ---
        partner_map = {}
        session_details = []
        if settings.get('dialogue_simulation_enabled') and len(sessions) >= 2:
            logger.info(f"{log_prefix}: Режим диалогов включен. Проверяю и спариваю сессии...")
            
            # Собираем информацию о каждой сессии, включая их peer_id
            for phone, s_path in sessions.items():
                s_name = os.path.splitext(os.path.basename(s_path))[0]
                temp_client = None
                try:
                    temp_client = await get_connected_client(user_id, s_name, no_updates=True)
                    if temp_client:
                        me = await temp_client.get_me()
                        if me and me.status != UserStatus.DEACTIVATED:
                            session_details.append({'phone': phone, 'session_name': s_name, 'peer_id': me.id})
                        await temp_client.disconnect()
                except Exception as e:
                    logger.warning(f"{log_prefix}: Не удалось проверить сессию {phone} для диалога: {e}")
                    if temp_client and temp_client.is_connected: await temp_client.disconnect()

            random.shuffle(session_details)
            
            # Создаем пары
            paired_sessions = deque(session_details)
            while len(paired_sessions) >= 2:
                s1 = paired_sessions.popleft()
                s2 = paired_sessions.popleft()
                partner_map[s1['session_name']] = s2['peer_id']
                partner_map[s2['session_name']] = s1['peer_id']
                logger.info(f"{log_prefix}: Создана пара для диалога: {s1['phone']} <-> {s2['phone']}")

        for phone, session_file_path in sessions.items(): # Запускаем воркеры для ВСЕХ сессий
            s_name = os.path.splitext(os.path.basename(session_file_path))[0]
            partner_id = partner_map.get(s_name)
            worker = asyncio.create_task(_warmer_worker(
                bot, user_id, s_name, phone, settings, stop_event, stats_lock,
                partner_peer_id=partner_id
            ))
            workers.append(worker)
        
        # Ожидаем либо сигнала остановки от пользователя, либо истечения таймера длительности
        duration_seconds = settings.get('duration_days', 7) * 24 * 3600
        try:
            logger.info(f"{log_prefix}: Прогрев будет длиться {settings.get('duration_days', 7)} дней.")
            await asyncio.wait_for(stop_event.wait(), timeout=duration_seconds)
            logger.info(f"{log_prefix}: Получен сигнал остановки от пользователя.")
        except asyncio.TimeoutError:
            logger.info(f"{log_prefix}: Запланированное время прогрева истекло. Завершаю работу.")
            # Устанавливаем событие, чтобы все воркеры корректно завершились
            if not stop_event.is_set():
                stop_event.set()

    except Exception as e:
        logger.critical(f"{log_prefix}: Критическая ошибка: {e}", exc_info=True)
    finally:
        logger.info(f"{log_prefix}: Завершение цикла прогрева.")
        
        # Cancel all worker tasks
        for task in workers:
            task.cancel()
        await asyncio.gather(*workers, return_exceptions=True)

        # Cleanup global state
        async with WARMER_STATUS_LOCK:
            WARMER_STATUS[user_id] = False
        WARMER_STOP_EVENTS.pop(user_id, None)
        ACTIVE_WARMER_TASKS.pop(user_id, None)
        
        async with stats_lock:
            final_stats = WARMER_STATS.pop(user_id, {})

        # Send final report
        report_message = (
            f"<b>🏁 Прогрев аккаунтов остановлен.</b>\n\n"
            f"<b>📈 Статистика:</b>\n"
            f"  - Выполнено действий: {final_stats.get('actions_done', 0)}\n"
            f"  - Ошибок: {final_stats.get('errors', 0)}\n"
        )
        failed = final_stats.get("failed_sessions", [])
        if failed:
            report_message += "\n<b>⚠️ Проблемные сессии:</b>\n"
            for f in failed:
                report_message += f"  - <code>{html.escape(f['phone'])}</code>: {html.escape(f['reason'])}\n"

        # --- ИЗМЕНЕНО: Добавлена обработка ошибок при отправке финального отчета ---
        try:
            await bot.send_message(user_id, report_message, reply_markup=warmer_menu_keyboard(is_active=False))
        except Exception as e:
            logger.error(f"{log_prefix}: Не удалось отправить финальный отчет пользователю {user_id}: {e}")

        logger.info(f"{log_prefix}: Цикл прогрева полностью завершен.")