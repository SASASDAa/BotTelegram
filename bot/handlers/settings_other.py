# bot/handlers/settings_other.py
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

import config
from bot.database.db_manager import db_manager
from bot.middlewares import check_subscription
from bot.keyboards import general_settings_keyboard
from bot.states import DelayStates

router = Router()

@router.message(F.text == "⚙️ Общие настройки")
async def general_settings_menu(message: Message):
    user_id = message.from_user.id
    text = "<b>⚙️ Общие настройки</b>\n\nЗдесь вы можете настроить поведение бота во время выполнения задач."
    markup = await general_settings_keyboard(user_id)
    await message.answer(text, reply_markup=markup)

@router.callback_query(F.data == "set_spam_delay")
async def set_delay_start_callback(query: CallbackQuery, state: FSMContext):
    # --- ДОБАВЛЕНО: Проверка подписки ---
    if not await check_subscription(query):
        return
    await query.answer()
    current_delay = await db_manager.get_delay(query.from_user.id)
    await query.message.edit_text(
        f"⏱ Текущая задержка между сообщениями в группах: {current_delay} сек.\n"
        f"Введите новую (минимум {config.MIN_DELAY_BETWEEN_COMMENTS}).\n"
        "Отправьте /cancel для отмены."
    )
    await state.update_data(general_settings_menu_id=query.message.message_id)
    await state.set_state(DelayStates.enter_delay)

@router.message(DelayStates.enter_delay)
async def set_delay_received(message: Message, state: FSMContext):
    try:
        delay = int(message.text)
        if delay < config.MIN_DELAY_BETWEEN_COMMENTS:
            await message.reply(f"❌ Мин. задержка - {config.MIN_DELAY_BETWEEN_COMMENTS} сек! Попробуйте снова или /cancel.")
            return

        user_id = message.from_user.id
        await db_manager.update_delay(user_id, delay)
        data = await state.get_data()
        menu_message_id = data.get("general_settings_menu_id")
        await state.clear()
        await message.delete()

        text = f"<b>⚙️ Общие настройки</b>\n\n✅ Задержка обновлена: {delay} сек."
        markup = await general_settings_keyboard(user_id)
        if menu_message_id:
            await message.bot.edit_message_text(chat_id=user_id, message_id=menu_message_id, text=text, reply_markup=markup)
        else:
            await message.answer(text, reply_markup=markup)

    except ValueError:
        await message.reply("❌ Введите целое число. Попробуйте снова или /cancel.")

@router.callback_query(F.data == "toggle_persistent_spam")
async def toggle_persistent_spam_callback(query: CallbackQuery):
    # --- ДОБАВЛЕНО: Проверка подписки ---
    if not await check_subscription(query):
        return
    user_id = query.from_user.id
    await query.answer()
    current_settings = await db_manager.get_ai_settings(user_id)
    new_status = not current_settings.get("persistent_spam", False)
    await db_manager.set_persistent_spam_enabled(user_id, new_status)
    markup = await general_settings_keyboard(user_id)
    await query.message.edit_reply_markup(reply_markup=markup)

@router.callback_query(F.data == "toggle_auto_leave")
async def toggle_auto_leave_callback(query: CallbackQuery):
    # --- ДОБАВЛЕНО: Проверка подписки ---
    if not await check_subscription(query):
        return
    user_id = query.from_user.id
    await query.answer()
    current_settings = await db_manager.get_ai_settings(user_id)
    new_status = not current_settings.get("auto_leave_enabled", False)
    await db_manager.set_auto_leave_enabled(user_id, new_status)
    markup = await general_settings_keyboard(user_id)
    await query.message.edit_reply_markup(reply_markup=markup)

@router.callback_query(F.data == "toggle_attack_skip_admins")
async def toggle_attack_skip_admins_callback(query: CallbackQuery):
    # --- ДОБАВЛЕНО: Проверка подписки ---
    if not await check_subscription(query):
        return
    user_id = query.from_user.id
    await query.answer()
    current_settings = await db_manager.get_ai_settings(user_id)
    new_status = not current_settings.get("attack_skip_admins", True)
    await db_manager.set_attack_skip_admins(user_id, new_status)
    markup = await general_settings_keyboard(user_id)
    await query.message.edit_reply_markup(reply_markup=markup)