# bot/handlers/attack_handler.py
import asyncio
import html
import logging
import os

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

import config
from bot.client_tasks.attack_loop import attack_loop_task
from bot.client_tasks.client_manager import (
    ATTACK_STATUS, ATTACK_STATUS_LOCK, ATTACK_STOP_EVENTS, SPAM_STATUS
)
from bot.database.db_manager import db_manager, DatabaseManager
from bot.keyboards import attack_menu_keyboard, select_sessions_keyboard, shop_keyboard, tasks_keyboard
from bot.middlewares import check_subscription
from bot.states import AttackStates

router = Router()
logger = logging.getLogger(__name__)

# --- Вспомогательные функции для меню ---

def get_attack_menu_text(data: dict) -> str:
    """Генерирует текст для меню настройки атаки в ЛС."""
    attack_mode = data.get('attack_mode', 'single')
    nick = data.get('attack_nickname')
    delay = data.get('attack_delay', 1.5)
    use_ai = data.get('attack_use_ai', False)
    is_infinite = data.get('attack_is_infinite', False)
    session_limit = data.get('attack_session_limit')
    skip_admins = data.get('attack_skip_admins', True)
    scraped_count = data.get('scraped_users_count', 0)

    if attack_mode == 'single':
        target_text = f"<b>1. Цель (юзер/группа):</b> {f'<code>{html.escape(nick)}</code>' if nick else '<i>не указана</i> ⚠️'}"
    else:
        target_text = f"<b>1. Цель:</b> Собранная база ({scraped_count} юзеров)"

    ai_text = "Вкл ✅" if use_ai else "Выкл ❌"
    count_val = data.get('attack_count', 1)
    count_text = "∞ (бесконечно)" if is_infinite else str(count_val)
    skip_admins_text = "Вкл ✅" if skip_admins else "Выкл ❌"

    return (
        f"<b>💥 Настройка атаки в ЛС</b>\n\n"
        f"Настройте параметры и нажмите 'Начать атаку'.\n\n"
        f"{target_text}\n"
        f"<b>2. Кол-во сообщений (на 1 цель):</b> {count_text}\n"
        f"<b>3. Задержка:</b> {delay} сек.\n"
        f"<b>4. Использовать ИИ:</b> {ai_text}\n"
        f"<b>5. Пропускать админов:</b> {skip_admins_text}"
    )


async def update_attack_menu(message: Message, state: FSMContext):
    """Обновляет сообщение с меню атаки."""
    data = await state.get_data()
    menu_message_id = data.get('attack_menu_message_id')
    if not menu_message_id:
        return

    try:
        await message.bot.edit_message_text(
            chat_id=message.chat.id,
            message_id=menu_message_id,
            text=get_attack_menu_text(data),
            reply_markup=attack_menu_keyboard(data)
        )
    except Exception as e:
        logger.error(f"Не удалось обновить меню атаки: {e}")

# --- Обработчики ---

@router.message(F.text == "💥 Атака в ЛС")
async def attack_by_nick_start(message: Message, state: FSMContext):
    user_id = message.from_user.id

    # --- ДОБАВЛЕНО: Проверка подписки в самом начале ---
    if not await check_subscription(message):
        return

    if ATTACK_STATUS.get(user_id, False):
        await message.reply("⚠️ Атака уже запущена!")
        return

    user_data = await db_manager.get_user_data(user_id)
    errors = []
    if not user_data['sessions']:
        errors.append("❌ Нет сессий для атаки.")
    if not await db_manager.get_comments(user_id):
        errors.append("❌ Нет текстов сообщений для атаки.")
    if errors:
        await message.reply("\n".join(errors))
        return

    # --- ИЗМЕНЕНО: Подсчет доступных, а не всех сессий ---
    # Local import to break circular dependency
    from bot.client_tasks.client_manager import (
        RESERVED_SESSIONS, RESERVED_SESSIONS_LOCK
    )
    all_sessions = user_data['sessions']
    async with RESERVED_SESSIONS_LOCK:
        reserved_for_user = RESERVED_SESSIONS.get(user_id, {})

    all_session_names = {os.path.splitext(os.path.basename(p))[0] for p in all_sessions.values()}
    available_session_names = all_session_names - set(reserved_for_user.keys())
    available_sessions_count = len(available_session_names)

    if available_sessions_count == 0:
        await message.reply("❌ Все сессии уже используются в других задачах.")
        return

    await state.set_state(AttackStates.select_sessions)
    await message.answer(
        f"Сколько сессий использовать для атаки? (Доступно: {available_sessions_count})",
        reply_markup=select_sessions_keyboard(available_sessions_count, 'attack')
    )


@router.callback_query(F.data.startswith("attack_sessions_"), AttackStates.select_sessions)
async def attack_sessions_selected(query: CallbackQuery, state: FSMContext):
    action = query.data.split('_')[-1]
    await query.answer()

    if action == 'custom':
        await state.set_state(AttackStates.set_session_count)
        await query.message.edit_text("Введите желаемое количество сессий:")
        return
    elif action == 'cancel':
        await query.message.delete()
        await state.clear()
        is_spam_active = SPAM_STATUS.get(query.from_user.id, False)
        await query.message.answer("Отменено.", reply_markup=tasks_keyboard(is_spam_active, False))
        return

    session_limit = None if action == 'all' else int(action)
    
    scraped_count = await db_manager.get_scraped_users_count(query.from_user.id)
    ai_settings = await db_manager.get_ai_settings(query.from_user.id)
    initial_data = {
        'attack_mode': 'single',
        'attack_count': 10, 'attack_delay': 1.5, 'attack_use_ai': False, 
        'attack_is_infinite': False, 'attack_session_limit': session_limit,
        'scraped_users_count': scraped_count,
        'attack_skip_admins': ai_settings.get('attack_skip_admins', True)
    }
    await state.set_data(initial_data)

    sent_message = await query.message.edit_text(
        get_attack_menu_text(initial_data),
        reply_markup=attack_menu_keyboard(initial_data)
    )
    await state.update_data(attack_menu_message_id=sent_message.message_id)
    await state.set_state(AttackStates.menu)


@router.message(AttackStates.set_session_count)
async def attack_sessions_custom_count(message: Message, state: FSMContext):
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

        # Переходим к основному меню настройки
        scraped_count = await db_manager.get_scraped_users_count(message.from_user.id)
        ai_settings = await db_manager.get_ai_settings(message.from_user.id)
        initial_data = {
            'attack_mode': 'single',
            'attack_count': 10, 'attack_delay': 1.5, 'attack_use_ai': False,
            'attack_is_infinite': False, 'attack_session_limit': session_limit,
            'scraped_users_count': scraped_count,
            'attack_skip_admins': ai_settings.get('attack_skip_admins', True)
        }
        await state.set_data(initial_data)

        sent_message = await message.answer(
            get_attack_menu_text(initial_data),
            reply_markup=attack_menu_keyboard(initial_data)
        )
        await state.update_data(attack_menu_message_id=sent_message.message_id)
        await state.set_state(AttackStates.menu)
    except (ValueError, TypeError):
        await message.reply("❌ Введите корректное число.")


@router.callback_query(AttackStates.menu)
async def attack_menu_router(query: CallbackQuery, state: FSMContext):
    action = query.data
    await query.answer()

    if action == "attack_set_nickname":
        await query.message.edit_text("👤 Введите ник цели (@username) или публичную ссылку на группу (@groupname).")
        await state.set_state(AttackStates.set_nickname)
    elif action == "attack_set_count":
        await query.message.edit_text("💬 Введите количество сообщений для отправки на каждую цель.")
        await state.set_state(AttackStates.set_count)
    elif action == "attack_set_delay":
        await query.message.edit_text(f"⏱ Введите задержку в секундах (мин: {config.MIN_DELAY_FOR_ATTACK}).")
        await state.set_state(AttackStates.set_delay)
    elif action == "attack_toggle_mode":
        data = await state.get_data()
        current_mode = data.get('attack_mode', 'single')
        new_mode = 'mass' if current_mode == 'single' else 'single'
        await state.update_data(attack_mode=new_mode)
        if new_mode == 'mass' and data.get('attack_count', 10) > 5:
            await state.update_data(attack_count=1) # Reset count to 1 for mass mode as a sensible default
        await update_attack_menu(query.message, state)
    elif action == "attack_toggle_ai":
        data = await state.get_data()
        await state.update_data(attack_use_ai=not data.get('attack_use_ai', False))
        await update_attack_menu(query.message, state)
    elif action == "attack_toggle_skip_admins":
        user_id = query.from_user.id
        data = await state.get_data()
        new_status = not data.get('attack_skip_admins', True)
        await db_manager.set_attack_skip_admins(user_id, new_status)
        await state.update_data(attack_skip_admins=new_status)
        await update_attack_menu(query.message, state)
    elif action == "attack_toggle_infinite":
        data = await state.get_data()
        await state.update_data(attack_is_infinite=not data.get('attack_is_infinite', False))
        await update_attack_menu(query.message, state)
    elif action == "attack_start":
        await start_attack_from_menu(query, state)
    elif action == "attack_cancel":
        user_id = query.from_user.id
        await query.message.delete()
        is_spam_active = SPAM_STATUS.get(user_id, False)
        is_attack_active = ATTACK_STATUS.get(user_id, False)
        await query.message.answer("Отменено.", reply_markup=tasks_keyboard(is_spam_active, is_attack_active))
        await state.clear()


async def start_attack_from_menu(query: CallbackQuery, state: FSMContext):
    user_id = query.from_user.id
    data = await state.get_data()
    attack_mode = data.get('attack_mode', 'single')

    async with ATTACK_STATUS_LOCK:
        if ATTACK_STATUS.get(user_id):
            await query.answer("⚠️ Атака уже запущена.", show_alert=True)
            return

        if attack_mode == 'single' and not data.get('attack_nickname'):
            await query.answer("❌ Укажите ник цели!", show_alert=True)
            return
        elif attack_mode == 'mass' and await db_manager.get_scraped_users_count(user_id) == 0:
            await query.answer("❌ Ваша база для рассылки пуста! Сначала соберите аудиторию.", show_alert=True)
            return

        # Все проверки пройдены, устанавливаем статус
        ATTACK_STATUS[user_id] = True

    await query.message.delete()
    await state.clear()

    ATTACK_STOP_EVENTS[user_id] = asyncio.Event()

    is_infinite = data.get('attack_is_infinite', False)
    session_limit = data.get('attack_session_limit')

    asyncio.create_task(attack_loop_task(
        user_id=user_id,
        bot=query.bot,
        attack_mode=attack_mode,
        target_nickname=data.get('attack_nickname'),
        message_count=data.get('attack_count', 1),
        attack_delay=data.get('attack_delay', 1.5),
        use_ai=data.get('attack_use_ai', False),
        is_infinite=is_infinite,
        session_limit=session_limit
    ))

    target_display = "по собранной базе" if attack_mode == 'mass' else f"на <code>{html.escape(data['attack_nickname'])}</code>"
    is_spam_active = SPAM_STATUS.get(user_id, False)
    await query.message.answer(
        f"💥 Атака в ЛС {target_display} запускается...",
        reply_markup=tasks_keyboard(is_spam_active=is_spam_active, is_attack_active=True)
    )


@router.message(AttackStates.set_nickname)
async def attack_receive_nickname(message: Message, state: FSMContext):
    target_input = message.text.strip()

    # Нормализуем ввод, чтобы Pyrogram правильно обработал полные ссылки
    # и обычные юзернеймы.
    normalized_target = target_input
    if normalized_target.startswith(('http://t.me/', 'https://t.me/')):
        path_part = normalized_target.split('t.me/')[1]
        # Нас интересуют только публичные группы/юзеры по юзернейму
        if not path_part.startswith(('joinchat', '+')):
            normalized_target = '@' + path_part.split('/')[0]
    elif not normalized_target.startswith('@'):
        normalized_target = '@' + normalized_target

    await state.update_data(attack_nickname=normalized_target)
    await message.delete()
    await update_attack_menu(message, state)
    await state.set_state(AttackStates.menu)


@router.message(AttackStates.set_count)
async def attack_receive_count(message: Message, state: FSMContext):
    try:
        count = int(message.text.strip())
        if count >= 1:
            await state.update_data(attack_count=count)
    except (ValueError, TypeError):
        pass # Игнорируем неверный ввод
    await message.delete()
    await update_attack_menu(message, state)
    await state.set_state(AttackStates.menu)


@router.message(AttackStates.set_delay)
async def attack_receive_delay(message: Message, state: FSMContext):
    try:
        delay = float(message.text.strip().replace(',', '.'))
        if delay >= config.MIN_DELAY_FOR_ATTACK:
            await state.update_data(attack_delay=delay)
    except (ValueError, TypeError):
        pass # Игнорируем неверный ввод
    await message.delete()
    await update_attack_menu(message, state)
    await state.set_state(AttackStates.menu)


@router.message(F.text == "🛑 Остановить атаку")
async def stop_attack_command(message: Message):
    user_id = message.from_user.id
    is_spam_active = SPAM_STATUS.get(user_id, False)

    if not ATTACK_STATUS.get(user_id):
        await message.reply("❌ Атака не была запущена.", reply_markup=tasks_keyboard(is_spam_active, False))
        return

    event = ATTACK_STOP_EVENTS.get(user_id)
    if event:
        event.set()
        await message.reply("🛑 Остановка атаки...", reply_markup=tasks_keyboard(is_spam_active, False))
    else:
        async with ATTACK_STATUS_LOCK:
            ATTACK_STATUS[user_id] = False
        await message.reply("Не найден активный процесс атаки. Статус сброшен.", reply_markup=tasks_keyboard(is_spam_active, False))