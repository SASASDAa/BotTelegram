# bot/handlers/common.py
import asyncio
import html

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from bot.database.db_manager import db_manager
from bot.keyboards import (main_keyboard, reset_keyboard, settings_keyboard, shop_keyboard,
                           tasks_keyboard)
from bot.middlewares import check_subscription

router = Router()

@router.message(CommandStart())
async def start_command(message: Message, state: FSMContext):
    await state.clear()
    user = message.from_user
    await db_manager.add_bot_user(user.id, user.username)
    await message.answer( # pragma: no cover
        f"👋 Привет, {user.first_name}! Я бот для отправки сообщений.\nИспользуйте кнопки для навигации.",
        reply_markup=main_keyboard()
    )

@router.callback_query(F.data == "noop_answer")
async def noop_answer_callback(query: CallbackQuery):
    """Handles callbacks from non-interactive buttons, just to acknowledge them."""
    await query.answer()

@router.message(Command("cancel"))
async def cancel_command(message: Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state is None:
        await message.answer("Нет активного действия для отмены.")
        return

    await state.clear()
    await message.answer("Действие отменено.", reply_markup=main_keyboard())


@router.message(F.text == "🔙 В меню")
async def back_to_main_menu(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Возврат в главное меню.", reply_markup=main_keyboard())


@router.message(F.text == "⚙️ Настройки")
async def settings_menu_command(message: Message):
    await message.answer("⚙️ Меню настроек:", reply_markup=settings_keyboard())


@router.message(F.text == "🚀 Задачи")
async def tasks_menu_command(message: Message):
    # Local import to break circular dependency
    from bot.client_tasks.client_manager import (
        SPAM_STATUS, ATTACK_STATUS
    )
    user_id = message.from_user.id
    is_spam = SPAM_STATUS.get(user_id, False)
    is_attack = ATTACK_STATUS.get(user_id, False)
    await message.answer("🚀 Меню управления задачами:", reply_markup=tasks_keyboard(is_spam, is_attack))


@router.message(F.text == "🛒 Магазин")
async def shop_menu_command(message: Message):
    text = (
        "<b>🛒 Магазин</b>\n\n"
        "Здесь вы можете приобрести дополнительные услуги.\n"
        "Для покупки свяжитесь с администратором."
    )
    await message.answer(text, reply_markup=await shop_keyboard())


@router.message(F.text == "🔄 Сброс данных")
async def reset_data_command(message: Message):
    await message.answer("🔄 Меню сброса данных:", reply_markup=reset_keyboard())


@router.message(F.text == "📊 Статус")
async def show_status_command(message: Message):
    # Local import to break circular dependency
    from bot.client_tasks.client_manager import (
        ATTACK_STATS, ATTACK_STATUS, RESERVED_SESSIONS, RESERVED_SESSIONS_LOCK,
        SPAM_STATS, SPAM_STATUS, WARMER_STATS, WARMER_STATUS
    )
    user_id = message.from_user.id
    user_data = await db_manager.get_user_data(user_id)
    chats_count = await db_manager.get_chats_count(user_id)
    comments = await db_manager.get_comments(user_id)
    delay = await db_manager.get_delay(user_id)
    ai_conf = await db_manager.get_ai_settings(user_id)
    sub_conf = await db_manager.get_subscription_status(user_id)
    auto_leave_status = 'Вкл ✅' if ai_conf.get('auto_leave_enabled') else 'Выкл ❌'
    persistent_spam_status = 'Вкл ✅' if ai_conf.get('persistent_spam') else 'Выкл ❌'

    # --- ИЗМЕНЕНО: Умный подсчет сессий ---
    total_sessions = len(user_data['sessions'])
    spam_sessions_count = 0
    attack_sessions_count = 0
    async with RESERVED_SESSIONS_LOCK:
        reserved_for_user = RESERVED_SESSIONS.get(user_id, {})
        for task_type in reserved_for_user.values():
            if task_type == 'spam':
                spam_sessions_count += 1
            elif task_type == 'attack':
                attack_sessions_count += 1
    
    used_sessions = spam_sessions_count + attack_sessions_count
    available_sessions = total_sessions - used_sessions
    
    sessions_status_parts = [f"Всего: {total_sessions}"]
    if used_sessions > 0:
        sessions_status_parts.append(f"Доступно: {available_sessions}")
    sessions_status_text = " | ".join(sessions_status_parts)

    status_text = (
        f"<b>📊 Статус для {message.from_user.mention_html()}:</b>\n\n"
        f"⭐ Подписка: {'Активна' if sub_conf['active'] else 'Неактивна'}\n"
        f"📱 Сессии: {sessions_status_text}\n"
        f"📢 Групп: {chats_count}\n"
        f"🌐 Прокси: {len(user_data['proxies'])}\n"
        f"💬 Текстов: {len(comments)}\n\n"
        f"<b>Настройки задач:</b>\n"
        f"⏱ Задержка (спам): {delay} сек.\n"
        f"🤖 Уникализация ИИ: {'Вкл ✅' if ai_conf.get('enabled') else 'Выкл ❌'}"
        f" (Ключ: {'Есть' if ai_conf.get('api_key') else 'Нет'})\n"
        f"🔁 Постоянный спам: {persistent_spam_status}\n"
        f"📤 Автовыход из групп: {auto_leave_status}\n\n"
    )
    if SPAM_STATUS.get(user_id, False):
        stats = SPAM_STATS.get(user_id, {})
        status_text += (
            f"<b>🚀 Спам в группы активен!</b>\n"
            f"   Отправлено: {stats.get('messages', 0)}\n"
            f"   Ошибок: {stats.get('errors', 0)}\n"
            f"   Сессий в работе: {stats.get('sessions_initial_count', '?')}\n\n"
        )
    else:
        status_text += "<i>💤 Спам в группы не активен.</i>\n\n"
    if ATTACK_STATUS.get(user_id, False):
        stats = ATTACK_STATS.get(user_id, {})
        safe_nick = html.escape(stats.get('nickname', 'N/A'))
        status_text += (
            f"<b>💥 Атака в ЛС активна!</b>\n"
            f"   Цель: <code>{safe_nick}</code>\n"
            f"   Отправлено: {stats.get('messages', 0)} / {stats.get('total_messages', '?')}\n"
            f"   Ошибок: {stats.get('errors', 0)}\n"
            f"   Сессий в работе: {stats.get('total_sessions', '?')}\n"
            f"   Задержка: {stats.get('delay', '?')} сек."
        )
    else:
        status_text += "<i>💤 Атака в ЛС не активна.</i>\n\n"

    if WARMER_STATUS.get(user_id, False):
        stats = WARMER_STATS.get(user_id, {})
        status_text += (
            f"<b>🔥 Прогрев аккаунтов активен!</b>\n"
            f"   Выполнено действий: {stats.get('actions_done', 0)}\n"
            f"   Ошибок: {stats.get('errors', 0)}\n"
            f"   Активных сессий: {stats.get('active_sessions', '?')}"
        )
    else:
        status_text += "<i>💤 Прогрев аккаунтов не активен.</i>"
    await message.answer(status_text)


# --- Reset Handlers ---
@router.message(F.text == "🗑️ Сессии")
async def reset_all_sessions_command(message: Message):
    if not await check_subscription(message):
        return
    await db_manager.reset_sessions(message.from_user.id)
    await message.answer("✅ Все сессии удалены.", reply_markup=reset_keyboard())

@router.message(F.text == "🗑️ Группы")
async def reset_all_chats_command(message: Message):
    if not await check_subscription(message):
        return
    await db_manager.reset_chats(message.from_user.id)
    await message.answer("✅ Список групп очищен.", reply_markup=reset_keyboard())

@router.message(F.text == "🗑️ Тексты")
async def reset_all_comments_command(message: Message):
    if not await check_subscription(message):
        return
    await db_manager.reset_comments(message.from_user.id)
    await message.answer("✅ Список текстов и прикрепленное фото очищены.", reply_markup=reset_keyboard())

@router.message(F.text == "🗑️ Прокси")
async def reset_all_proxies_command(message: Message):
    if not await check_subscription(message):
        return
    await db_manager.reset_proxies(message.from_user.id)
    await message.answer("✅ Список прокси очищен.", reply_markup=reset_keyboard())

@router.message(F.text == "🗑️ Всё")
async def reset_everything_command(message: Message):
    if not await check_subscription(message):
        return
    user_id = message.from_user.id
    # Local import to break circular dependency
    from bot.client_tasks.client_manager import (
        SPAM_STATUS, ATTACK_STATUS, STOP_EVENTS, ATTACK_STOP_EVENTS
    )
    stopped = False
    if SPAM_STATUS.get(user_id, False):
        event = STOP_EVENTS.pop(user_id, None)
        if event: event.set()
        stopped = True
    if ATTACK_STATUS.get(user_id, False):
        event = ATTACK_STOP_EVENTS.pop(user_id, None)
        if event: event.set()
        stopped = True
    if stopped:
        await message.answer("🛑 Все активные задачи будут остановлены перед полным сбросом...")
        await asyncio.sleep(2)
    await db_manager.reset_sessions(user_id)
    await db_manager.reset_chats(user_id)
    await db_manager.reset_comments(user_id)
    await db_manager.reset_proxies(user_id)
    await db_manager.reset_scraped_users(user_id)
    await db_manager.update_delay(user_id, 20) # Reset to default
    await message.answer("✅ Все данные (сессии, группы, тексты, прокси, собранные юзеры, задержка) сброшены!", reply_markup=main_keyboard())