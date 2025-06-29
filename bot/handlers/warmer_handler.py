# bot/handlers/warmer_handler.py
import asyncio
import html
import logging
import re

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.client_tasks.client_manager import (
    ACTIVE_WARMER_TASKS, WARMER_STATUS, WARMER_STATUS_LOCK,
    WARMER_STOP_EVENTS
)
from bot.client_tasks.warmer_loop import warmer_loop_task
from bot.database.db_manager import db_manager
from bot.keyboards import (
    warmer_menu_keyboard, warmer_settings_main_keyboard, warmer_settings_limits_keyboard,
    warmer_settings_content_keyboard, warmer_settings_behavior_keyboard
)
from bot.middlewares import check_subscription
from bot.states import WarmerStates

router = Router()
logger = logging.getLogger(__name__)

TUTORIAL_TEXT = (
    "📖 <b>Что такое \"прогрев\" аккаунтов и зачем он нужен?</b>\n\n"
    "\"Прогрев\" — это процесс имитации действий обычного пользователя для повышения доверия "
    "к вашим Telegram-аккаунтам (сессиям).\n\n"
    "<b>Зачем это нужно?</b>\n"
    "Telegram активно борется со спамом и автоматизацией. Новые или неактивные аккаунты, "
    "которые внезапно начинают массово вступать в группы или отправлять сообщения, "
    "быстро получают ограничения или блокировку (бан).\n\n"
    "<b>Что делает прогрев?</b>\n"
    "Бот в течение нескольких дней будет от имени ваших сессий выполнять \"человеческие\" действия:\n"
    "  • Вступать в популярные каналы по вашим ключевым словам.\n"
    "  • Ставить реакции на посты.\n\n"
    "Бот также может имитировать <b>переписку между вашими аккаунтами</b> и работать по "
    "<b>\"человеческому\" расписанию</b> (например, только днем), чтобы максимально повысить "
    "доверие со стороны Telegram.\n\n"
    "Это создает видимость естественного использования аккаунта, что <b>значительно снижает риск блокировки</b> "
    "и повышает \"живучесть\" ваших сессий.\n\n"
    "<b>Как использовать?</b>\n"
    "1. Зайдите в \"Настройки прогрева\" и задайте желаемые параметры.\n"
    "2. Нажмите \"Начать прогрев\".\n"
    "3. Бот запустит фоновую задачу, которая будет работать несколько дней. Вы можете остановить ее в любой момент."
)

@router.message(F.text == "🔥 Прогрев")
async def warmer_main_menu(message: Message):
    is_active = WARMER_STATUS.get(message.from_user.id, False)
    await message.answer(
        "Меню прогрева аккаунтов. Этот процесс повышает доверие к вашим сессиям, снижая риск бана.",
        reply_markup=warmer_menu_keyboard(is_active)
    )

@router.message(F.text == "📖 Что такое прогрев?")
async def warmer_tutorial(message: Message):
    await message.answer(TUTORIAL_TEXT, reply_markup=warmer_menu_keyboard(WARMER_STATUS.get(message.from_user.id, False)))

@router.message(F.text.in_({"🔥 Начать прогрев", "🛑 Остановить прогрев"}))
async def start_stop_warmer(message: Message):
    user_id = message.from_user.id

    if message.text == "🔥 Начать прогрев":
        # --- ИЗМЕНЕНО: Используем новую общую функцию проверки ---
        if not await check_subscription(message):
            return
        async with WARMER_STATUS_LOCK:
            if WARMER_STATUS.get(user_id, False):
                await message.reply("⚠️ Прогрев уже запущен!")
                return

            WARMER_STATUS[user_id] = True

        WARMER_STOP_EVENTS[user_id] = asyncio.Event()
        task = asyncio.create_task(warmer_loop_task(message.bot, user_id))
        ACTIVE_WARMER_TASKS[user_id] = task
        await message.reply(
            "✅ Прогрев запущен в фоновом режиме. Он будет работать несколько дней согласно настройкам.",
            reply_markup=warmer_menu_keyboard(is_active=True)
        )
    else: # Stop
        if not WARMER_STATUS.get(user_id):
            await message.reply("❌ Прогрев не был запущен.")
            return

        event = WARMER_STOP_EVENTS.get(user_id)
        if event:
            event.set()
            await message.reply(
                "🛑 Посылаю сигнал остановки прогрева... Дождитесь отчета о завершении.",
                reply_markup=warmer_menu_keyboard(is_active=False)
            )
        else:
            async with WARMER_STATUS_LOCK:
                WARMER_STATUS[user_id] = False
            await message.reply("Не найден активный процесс прогрева. Статус сброшен.", reply_markup=warmer_menu_keyboard(is_active=False))

@router.message(F.text == "⚙️ Настройки прогрева")
async def warmer_settings_menu(message: Message, state: FSMContext):
    settings = await db_manager.get_warmer_settings(message.from_user.id)
    sent_message = await message.answer(
        "Тонкая настройка параметров прогрева:",
        reply_markup=warmer_settings_main_keyboard(settings)
    )
    await state.set_state(WarmerStates.menu_main)
    await state.update_data(menu_message_id=sent_message.message_id)

async def _update_settings_menu_view(bot: Bot, chat_id: int, state: FSMContext, menu_to_show: str = None):
    """Helper to edit the menu message with fresh data, handling potential errors."""
    data = await state.get_data()
    menu_id = data.get("menu_message_id")
    if not menu_id: return

    try:
        # Если конкретное меню не указано, определяем его по текущему состоянию FSM
        if not menu_to_show:
            current_state = await state.get_state()
            state_map = {
                WarmerStates.menu_limits: 'limits',
                WarmerStates.menu_content: 'content',
                WarmerStates.menu_behavior: 'behavior',
            }
            menu_to_show = state_map.get(current_state, 'main')

        settings = await db_manager.get_warmer_settings(chat_id)
        text = "Тонкая настройка параметров прогрева:"
        
        keyboard_map = {
            'main': warmer_settings_main_keyboard(settings),
            'limits': warmer_settings_limits_keyboard(settings),
            'content': warmer_settings_content_keyboard(settings),
            'behavior': warmer_settings_behavior_keyboard(settings),
        }
        markup = keyboard_map.get(menu_to_show)

        await bot.edit_message_text(text=text, chat_id=chat_id, message_id=menu_id, reply_markup=markup)
    except Exception as e:
        logger.warning(f"Could not update warmer settings menu (msg: {menu_id}) for user {chat_id}: {e}")

# --- НАВИГАЦИЯ ПО МЕНЮ НАСТРОЕК ---

@router.callback_query(F.data.startswith("warmer_show_"), WarmerStates.menu_main)
async def warmer_show_submenu(query: CallbackQuery, state: FSMContext):
    """Переключает на одно из подменю."""
    await query.answer()
    submenu_type = query.data.split('_')[-1] # limits, content, behavior
    
    state_map = {
        'limits': WarmerStates.menu_limits,
        'content': WarmerStates.menu_content,
        'behavior': WarmerStates.menu_behavior,
    }
    new_state = state_map.get(submenu_type)
    
    if new_state:
        await state.set_state(new_state)
        await _update_settings_menu_view(query.bot, query.from_user.id, state, menu_to_show=submenu_type)

@router.callback_query(F.data == "warmer_back_to_main") # Работает из любого состояния
async def warmer_back_to_main_menu(query: CallbackQuery, state: FSMContext):
    """Возвращает в главное меню настроек."""
    await query.answer()
    await state.set_state(WarmerStates.menu_main)
    await _update_settings_menu_view(query.bot, query.from_user.id, state, menu_to_show='main')

# --- ОБРАБОТЧИКИ ДЕЙСТВИЙ ---

@router.callback_query(F.data == "warmer_toggle_inform", WarmerStates.menu_behavior)
async def toggle_inform_callback(query: CallbackQuery, state: FSMContext):
    # --- ДОБАВЛЕНО: Проверка подписки ---
    if not await check_subscription(query):
        return
    user_id = query.from_user.id
    await query.answer()
    current_settings = await db_manager.get_warmer_settings(user_id)
    new_status = not current_settings.get("inform_user_on_action", False)
    await db_manager.update_warmer_settings(user_id, {"inform_user_on_action": new_status})
    await _update_settings_menu_view(query.bot, user_id, state, menu_to_show='behavior')

@router.callback_query(F.data == "warmer_toggle_dialogue", WarmerStates.menu_behavior)
async def toggle_dialogue_callback(query: CallbackQuery, state: FSMContext):
    # --- ДОБАВЛЕНО: Проверка подписки ---
    if not await check_subscription(query):
        return
    user_id = query.from_user.id
    await query.answer()
    current_settings = await db_manager.get_warmer_settings(user_id)
    new_status = not current_settings.get("dialogue_simulation_enabled", False)
    await db_manager.update_warmer_settings(user_id, {"dialogue_simulation_enabled": new_status})
    await _update_settings_menu_view(query.bot, user_id, state, menu_to_show='behavior')

@router.callback_query(F.data == "warmer_toggle_schedule", WarmerStates.menu_behavior)
async def toggle_schedule_callback(query: CallbackQuery, state: FSMContext):
    # --- ДОБАВЛЕНО: Проверка подписки ---
    if not await check_subscription(query):
        return
    user_id = query.from_user.id
    await query.answer()
    current_settings = await db_manager.get_warmer_settings(user_id)
    new_status = not current_settings.get("active_hours_enabled", False)
    await db_manager.update_warmer_settings(user_id, {"active_hours_enabled": new_status})
    await _update_settings_menu_view(query.bot, user_id, state, menu_to_show='behavior')

@router.callback_query(F.data.startswith("warmer_set_")) # Работает из любого состояния
async def warmer_settings_fsm_start(query: CallbackQuery, state: FSMContext):
    # --- ДОБАВЛЕНО: Проверка подписки ---
    if not await check_subscription(query):
        return
    action = query.data.split('_')[-1]
    prompts = {
        "duration": ("⏳ Введите длительность прогрева в днях (например, 7).", WarmerStates.set_duration),
        "joins": ("📥 Введите макс. кол-во вступлений в каналы в день для одной сессии (например, 2).", WarmerStates.set_joins),
        "reactions": ("👍 Введите макс. кол-во реакций в день для одной сессии (например, 5).", WarmerStates.set_reactions),
        "dialogues": ("💬 Введите макс. кол-во сообщений в диалогах в день для одной сессии (например, 3).", WarmerStates.set_dialogues),
        "channels": ("🎯 Введите через запятую или с новой строки ссылки/юзернеймы каналов для вступления (например, @durov, https://t.me/telegram).", WarmerStates.set_target_channels),
        "phrases": ("📝 Введите фразы для диалогов через запятую (например: Привет,Как дела?,Все ок).", WarmerStates.set_dialogue_phrases),
        "schedule": ("⏰ Введите диапазон рабочих часов (по МСК) в формате <b>ЧАС_СТАРТА-ЧАС_КОНЦА</b> (например, <b>9-22</b>).", WarmerStates.set_active_hours),
    }
    prompt_text, next_state = prompts.get(action, (None, None))
    if not prompt_text:
        await query.answer("Неизвестное действие.")
        return

    # Сохраняем текущее состояние, чтобы вернуться в правильное меню
    await state.update_data(return_state=await state.get_state())
    await state.set_state(next_state)
    await query.message.edit_text(prompt_text + "\n\n/cancel для отмены.")
    await query.answer()

@router.message(WarmerStates.set_duration, F.text)
async def process_warmer_duration(message: Message, state: FSMContext):
    try:
        days = int(message.text)
        if not 1 <= days <= 30:
            raise ValueError
        user_id = message.from_user.id
        await db_manager.update_warmer_settings(user_id, {"duration_days": days})
        await message.delete()
        data = await state.get_data()
        return_state = data.get('return_state', WarmerStates.menu_main)
        await state.set_state(return_state)
        await _update_settings_menu_view(message.bot, user_id, state)
    except (ValueError, TypeError):
        await message.reply("❌ Введите число от 1 до 30.")

@router.message(WarmerStates.set_joins, F.text)
async def process_warmer_joins(message: Message, state: FSMContext):
    try:
        count = int(message.text)
        if not 0 <= count <= 10: raise ValueError
        user_id = message.from_user.id
        await db_manager.update_warmer_settings(user_id, {"join_channels_per_day": count})
        await message.delete()
        data = await state.get_data()
        return_state = data.get('return_state', WarmerStates.menu_main)
        await state.set_state(return_state)
        await _update_settings_menu_view(message.bot, user_id, state)
    except (ValueError, TypeError):
        await message.reply("❌ Введите число от 0 до 10.")

@router.message(WarmerStates.set_reactions, F.text)
async def process_warmer_reactions(message: Message, state: FSMContext):
    try:
        count = int(message.text)
        if not 0 <= count <= 20: raise ValueError
        user_id = message.from_user.id
        await db_manager.update_warmer_settings(user_id, {"send_reactions_per_day": count})
        await message.delete()
        data = await state.get_data()
        return_state = data.get('return_state', WarmerStates.menu_main)
        await state.set_state(return_state)
        await _update_settings_menu_view(message.bot, user_id, state)
    except (ValueError, TypeError):
        await message.reply("❌ Введите число от 0 до 20.")

@router.message(WarmerStates.set_dialogues, F.text)
async def process_warmer_dialogues(message: Message, state: FSMContext):
    try:
        count = int(message.text)
        if not 0 <= count <= 20: raise ValueError
        user_id = message.from_user.id
        await db_manager.update_warmer_settings(user_id, {"dialogues_per_day": count})
        await message.delete()
        data = await state.get_data()
        return_state = data.get('return_state', WarmerStates.menu_main)
        await state.set_state(return_state)
        await _update_settings_menu_view(message.bot, user_id, state)
    except (ValueError, TypeError):
        await message.reply("❌ Введите число от 0 до 20.")

@router.message(WarmerStates.set_target_channels, F.text)
async def process_warmer_target_channels(message: Message, state: FSMContext):
    raw_channels_input = message.text.strip()
    if not raw_channels_input:
        await message.reply("❌ Список каналов не может быть пустым.")
        return

    # --- ДОБАВЛЕНО: Нормализация ссылок перед сохранением в БД ---
    # Это делает обработку консистентной с другими частями бота (добавление групп для спама).
    # Даже при наличии нормализации в воркере, лучше хранить в БД уже обработанные данные.
    raw_channels_list = re.split(r'[,\n]', raw_channels_input)
    normalized_channels = []
    for channel_input in raw_channels_list:
        clean_input = channel_input.strip()
        if not clean_input:
            continue

        if clean_input.startswith(('http://t.me/', 'https://t.me/')):
            path_part = clean_input.split('t.me/')[1]
            if not path_part.startswith(('joinchat', '+')):
                # Это ссылка на публичный канал, нормализуем в @username
                normalized_channels.append('@' + path_part.split('/')[0])
                continue

        normalized_channels.append(clean_input)

    user_id = message.from_user.id
    final_channels_text = ", ".join(normalized_channels)
    await db_manager.update_warmer_settings(user_id, {"target_channels": final_channels_text})
    await message.delete()
    data = await state.get_data()
    return_state = data.get('return_state', WarmerStates.menu_main)
    await state.set_state(return_state)
    await _update_settings_menu_view(message.bot, user_id, state)

@router.message(WarmerStates.set_dialogue_phrases, F.text)
async def process_warmer_dialogue_phrases(message: Message, state: FSMContext):
    phrases = message.text.strip()
    if not phrases:
        await message.reply("❌ Список фраз не может быть пустым.")
        return
    user_id = message.from_user.id
    await db_manager.update_warmer_settings(user_id, {"dialogue_phrases": phrases})
    await message.delete()
    data = await state.get_data()
    return_state = data.get('return_state', WarmerStates.menu_main)
    await state.set_state(return_state)
    await _update_settings_menu_view(message.bot, user_id, state)

@router.message(WarmerStates.set_active_hours, F.text)
async def process_warmer_active_hours(message: Message, state: FSMContext):
    match = re.match(r"^\s*(\d{1,2})\s*-\s*(\d{1,2})\s*$", message.text)
    if not match:
        await message.reply("❌ Неверный формат. Введите часы в формате <b>СТАРТ-КОНЕЦ</b>, например: <b>9-22</b>.")
        return
    
    start_h, end_h = int(match.group(1)), int(match.group(2))
    if not (0 <= start_h <= 23 and 0 <= end_h <= 23):
        await message.reply("❌ Часы должны быть в диапазоне от 0 до 23.")
        return