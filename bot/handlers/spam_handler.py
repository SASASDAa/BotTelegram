# bot/handlers/spam_handler.py
import asyncio
import logging
import os

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.client_tasks.client_manager import (
    ACTIVE_SPAM_TASKS, ATTACK_STATUS, SPAM_STATUS, SPAM_STATUS_LOCK,
    STOP_EVENTS
)
from bot.client_tasks.spam_loop import spam_loop_task
from bot.database.db_manager import db_manager
from bot.middlewares import check_subscription
from bot.keyboards import select_sessions_keyboard, tasks_keyboard
from bot.states import SpamStates

router = Router()
logger = logging.getLogger(__name__)

async def _actually_start_spam_task(message: Message, bot: Bot, user_id: int, session_limit: int | None, state: FSMContext):
    """Внутренняя функция для фактического запуска задачи спама после всех настроек."""
    # --- ИЗМЕНЕНО: Добавлен лок для атомарной проверки и установки статуса ---
    await state.clear()
    async with SPAM_STATUS_LOCK:
        if SPAM_STATUS.get(user_id):
            await message.answer("⚠️ Спам уже запущен (обнаружено во время финальной проверки).")
            return

        # Все проверки пройдены, устанавливаем статус и выходим из-под лока
        SPAM_STATUS[user_id] = True

    is_attack_active = ATTACK_STATUS.get(user_id, False)
    status_message = await message.answer(
        "▶️ <b>Спам-сессия запущена.</b>\n\n"
        "Вы можете отслеживать прогресс в меню 📊 <b>Статус</b>. По завершении вы получите итоговый отчет.",
        reply_markup=tasks_keyboard(is_spam_active=True, is_attack_active=is_attack_active)
    )
    stop_event = asyncio.Event()
    STOP_EVENTS[user_id] = stop_event

    # Создаем и сохраняем задачу, передавая ID сообщения для статуса
    task = asyncio.create_task(spam_loop_task(
        user_id=user_id,
        bot=bot,
        status_chat_id=status_message.chat.id,
        status_message_id=status_message.message_id,
        session_limit=session_limit
    ))
    ACTIVE_SPAM_TASKS[user_id] = task

@router.message(F.text == "▶️ Спам в группы")
async def start_spam_command(message: Message, state: FSMContext):
    user_id = message.from_user.id

    # --- ДОБАВЛЕНО: Проверка подписки в самом начале ---
    if not await check_subscription(message):
        return

    if SPAM_STATUS.get(user_id):
        is_attack_active = ATTACK_STATUS.get(user_id, False)
        await message.answer(
            "Спам уже запущен.",
            reply_markup=tasks_keyboard(is_spam_active=True, is_attack_active=is_attack_active)
        )
        return

    # --- ИЗМЕНЕНО: Добавлены проверки на наличие всего необходимого для спама ---
    all_user_sessions_data = await db_manager.get_user_data(user_id)
    all_sessions = all_user_sessions_data['sessions']
    chats_count = await db_manager.get_chats_count(user_id)
    comments = await db_manager.get_comments(user_id)

    errors = []
    if not all_sessions:
        errors.append("❌ Нет сессий для запуска спама.")
    if chats_count == 0:
        errors.append("❌ Нет групп для спама. Добавьте их в настройках.")
    if not comments:
        errors.append("❌ Нет текстов для спама. Добавьте их в настройках.")
    if errors:
        await message.reply("\n".join(errors))
        return

    # --- ИЗМЕНЕНО: Подсчет доступных, а не всех сессий ---
    # Local import to break circular dependency
    from bot.client_tasks.client_manager import (
        RESERVED_SESSIONS, RESERVED_SESSIONS_LOCK
    )
    async with RESERVED_SESSIONS_LOCK:
        reserved_for_user = RESERVED_SESSIONS.get(user_id, {})

    all_session_names = {os.path.splitext(os.path.basename(p))[0] for p in all_sessions.values()}
    available_session_names = all_session_names - set(reserved_for_user.keys())
    available_sessions_count = len(available_session_names)

    if available_sessions_count == 0:
        await message.reply("❌ Все сессии уже используются в других задачах.")
        return

    await state.set_state(SpamStates.select_sessions)
    await message.answer(
        f"Сколько сессий использовать для спама? (Доступно: {available_sessions_count})",
        reply_markup=select_sessions_keyboard(available_sessions_count, 'spam')
    )

@router.callback_query(F.data.startswith("spam_sessions_"), SpamStates.select_sessions)
async def spam_sessions_selected(query: CallbackQuery, state: FSMContext, bot: Bot):
    action = query.data.split('_')[-1]
    await query.answer()

    if action == 'custom':
        await state.set_state(SpamStates.set_session_count)
        await query.message.edit_text("Введите желаемое количество сессий:")
        return
    elif action == 'cancel':
        await query.message.delete()
        await state.clear()
        is_attack_active = ATTACK_STATUS.get(query.from_user.id, False)
        await query.message.answer("Отменено.", reply_markup=tasks_keyboard(False, is_attack_active))
        return

    session_limit = None if action == 'all' else int(action)
    await query.message.delete()
    await _actually_start_spam_task(query.message, bot, query.from_user.id, session_limit, state)

@router.message(SpamStates.set_session_count)
async def spam_sessions_custom_count(message: Message, state: FSMContext, bot: Bot):
    try:
        session_limit = int(message.text)
        user_id = message.from_user.id

        # --- ИЗМЕНЕНО: Проверка по доступным сессиям ---
        # Local import to break circular dependency
        from bot.client_tasks.client_manager import (
            RESERVED_SESSIONS, RESERVED_SESSIONS_LOCK
        )
        all_user_sessions_data = await db_manager.get_user_data(user_id)
        all_sessions = all_user_sessions_data['sessions']
        async with RESERVED_SESSIONS_LOCK:
            reserved_for_user = RESERVED_SESSIONS.get(user_id, {})

        all_session_names = {os.path.splitext(os.path.basename(p))[0] for p in all_sessions.values()}
        available_session_names = all_session_names - set(reserved_for_user.keys())
        available_sessions_count = len(available_session_names)

        if not (0 < session_limit <= available_sessions_count):
            await message.reply(f"❌ Введите число от 1 до {available_sessions_count}.")
            return
        await _actually_start_spam_task(message, bot, user_id, session_limit, state)
    except (ValueError, TypeError):
        await message.reply("❌ Введите корректное число.")

@router.message(F.text == "🛑 Остановить спам")
async def stop_spam_command(message: Message):
    user_id = message.from_user.id
    is_attack_active = ATTACK_STATUS.get(user_id, False)

    if not SPAM_STATUS.get(user_id, False):
        await message.answer(
            "Спам не запущен.",
            reply_markup=tasks_keyboard(is_spam_active=False, is_attack_active=is_attack_active)
        )
        return

    stop_event = STOP_EVENTS.get(user_id)
    if stop_event:
        stop_event.set()
        # Клавиатура обновится после того, как задача завершится и отправит отчет
        await message.answer("🛑 Посылаю сигнал остановки спама... Дождитесь отчета о завершении.")
    else:
        # Аварийный случай, если статус есть, а события нет
        async with SPAM_STATUS_LOCK:
            SPAM_STATUS[user_id] = False
        await message.answer(
            "Не найден активный процесс спама. Статус сброшен.",
            reply_markup=tasks_keyboard(is_spam_active=False, is_attack_active=is_attack_active)
        )