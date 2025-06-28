# bot/handlers/settings_ai.py
import html

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.database.db_manager import db_manager
from bot.middlewares import check_subscription
from bot.states import AiStates
from ..keyboards import ai_settings_keyboard, settings_keyboard

router = Router()

async def get_ai_menu_text(user_id: int, status_line: str = "") -> str:
    ai_conf = await db_manager.get_ai_settings(user_id)
    api_key_status = "Установлен" if ai_conf["api_key"] else "Не установлен"
    prompt_to_show = ai_conf.get("prompt")
    prompt_status = f"<code>{html.escape(prompt_to_show[:30])}...</code>" if prompt_to_show else "Не установлен"
    enabled_status = "Включена" if ai_conf["enabled"] else "Выключена"
    text = (
        f"<b>🤖 Настройки Уникализации Сообщений (Gemini):</b>\n\n"
        f"{status_line}\n"
        f"▫️ API Ключ: {api_key_status}\n"
        f"▫️ Промпт: {prompt_status}\n"
        f"▫️ Статус: {enabled_status}\n\n"
        "Выберите действие:"
    )
    return text

@router.message(F.text == "🤖 Настройки ИИ")
async def ai_settings_menu_command(message: Message):
    user_id = message.from_user.id
    text = await get_ai_menu_text(user_id)
    markup = await ai_settings_keyboard(user_id)
    await message.answer(text, reply_markup=markup)

@router.callback_query(F.data == "set_gemini_key")
async def set_gemini_key_start(query: CallbackQuery, state: FSMContext):
    # --- ДОБАВЛЕНО: Проверка подписки ---
    if not await check_subscription(query):
        return
    await query.answer()
    await query.message.edit_text(
        "🔑 Введите ваш API ключ для Google Gemini (AI Studio).\n"
        "Отправьте /cancel для отмены.\nApi ключ можно получить здесь: https://aistudio.google.com/apikey"
    )
    await state.update_data(ai_menu_message_id=query.message.message_id)
    await state.set_state(AiStates.set_key)

@router.message(AiStates.set_key)
async def set_gemini_key_received(message: Message, state: FSMContext):
    api_key = message.text.strip()
    user_id = message.from_user.id
    data = await state.get_data()
    menu_message_id = data.get("ai_menu_message_id")
    await message.delete() # Удаляем сообщение с ключом
    await state.clear()

    if not api_key or len(api_key) < 20:
        status_line = "❌ Неверный формат API ключа."
    else:
        await db_manager.set_gemini_api_key(user_id, api_key)
        status_line = "✅ API ключ Gemini сохранен."

    text = await get_ai_menu_text(user_id, status_line=status_line)
    markup = await ai_settings_keyboard(user_id)
    if menu_message_id:
        try:
            await message.bot.edit_message_text(
                chat_id=user_id, message_id=menu_message_id, text=text, reply_markup=markup
            )
        except Exception:
            await message.answer(text, reply_markup=markup)
    else:
        await message.answer(text, reply_markup=markup)


@router.callback_query(F.data == "set_gemini_prompt")
async def set_gemini_prompt_start(query: CallbackQuery, state: FSMContext):
    # --- ДОБАВЛЕНО: Проверка подписки ---
    if not await check_subscription(query):
        return
    await query.answer()
    current_prompt = (await db_manager.get_ai_settings(query.from_user.id))["prompt"]
    await query.message.edit_text(
        f"📝 Введите новый промпт для уникализации текста. \n"
        f"Текущий промпт: <code>{html.escape(current_prompt)}</code>\n"
        "Промпт должен содержать инструкцию для ИИ. "
        "В конец промпта будет добавлен оригинальный текст.\n"
        "Отправьте /cancel для отмены."
    )
    await state.update_data(ai_menu_message_id=query.message.message_id)
    await state.set_state(AiStates.set_prompt)

@router.message(AiStates.set_prompt)
async def set_gemini_prompt_received(message: Message, state: FSMContext):
    prompt = message.text.strip()
    user_id = message.from_user.id
    data = await state.get_data()
    menu_message_id = data.get("ai_menu_message_id")
    await message.delete()
    await state.clear()

    if not prompt:
        status_line = "❌ Промпт не может быть пустым."
    else:
        await db_manager.set_uniqueness_prompt(user_id, prompt)
        status_line = "✅ Промпт для Gemini сохранен."

    text = await get_ai_menu_text(user_id, status_line=status_line)
    markup = await ai_settings_keyboard(user_id)
    if menu_message_id:
        try:
            await message.bot.edit_message_text(
                chat_id=user_id, message_id=menu_message_id, text=text, reply_markup=markup
            )
        except Exception:
            await message.answer(text, reply_markup=markup)
    else:
        await message.answer(text, reply_markup=markup)


@router.callback_query(F.data == "toggle_uniqueness")
async def toggle_uniqueness_callback(query: CallbackQuery):
    # --- ДОБАВЛЕНО: Проверка подписки ---
    if not await check_subscription(query):
        return
    user_id = query.from_user.id
    await query.answer()
    ai_settings = await db_manager.get_ai_settings(user_id)
    new_status = not ai_settings["enabled"]
    await db_manager.set_uniqueness_enabled(user_id, new_status)

    text = await get_ai_menu_text(user_id)
    markup = await ai_settings_keyboard(user_id)
    await query.message.edit_text(text, reply_markup=markup)