# bot/handlers/admin.py
import asyncio
import html
import uuid
from typing import Callable

from aiogram import F, Bot, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

import config
from bot.client_tasks.broadcast import broadcast_task
from bot.filters import IsAdminFilter, IsSuperAdminFilter
from bot.database.db_manager import db_manager
from bot.keyboards import (
    # ... (imports)
    admin_ban_confirm_keyboard, admin_broadcast_confirm_keyboard, admin_shop_settings_keyboard,
    admin_keyboard, admin_promo_list_keyboard, admin_promo_menu_keyboard, admin_restart_confirm_keyboard,
    admin_promo_type_keyboard, manage_admins_keyboard, main_keyboard, InlineKeyboardMarkup, InlineKeyboardButton)
from bot.states import AdminStates

router = Router()

router.message.filter(IsAdminFilter())
router.callback_query.filter(IsAdminFilter())

@router.message(Command("admin"))
async def admin_panel_command(message: Message):
    is_super = message.from_user.id == config.SUPER_ADMIN_ID
    await message.answer("Добро пожаловать в панель администратора!", reply_markup=admin_keyboard(is_super_admin=is_super))

@router.message(F.text == "⬅️ Назад в админ-меню")
async def back_to_admin_menu(message: Message, state: FSMContext):
    await state.clear()
    is_super = message.from_user.id == config.SUPER_ADMIN_ID
    await message.answer("Панель администратора.", reply_markup=admin_keyboard(is_super_admin=is_super))

@router.message(F.text == "📊 Статистика бота")
async def bot_stats_command(message: Message):
    stats = await db_manager.get_bot_stats()
    text = (
        f"<b>📊 Статистика бота</b>\n\n"
        f"▫️ Всего пользователей: {stats['total_users']}\n"
        f"▫️ Активных подписок: {stats['active_subscriptions']}"
    )
    await message.answer(text)

# --- Рассылка ---
@router.message(F.text == "📢 Рассылка")
async def broadcast_start_command(message: Message, state: FSMContext):
    await state.set_state(AdminStates.broadcast_message)
    await message.answer("Введите текст для рассылки. /cancel для отмены.")

@router.message(AdminStates.broadcast_message)
async def broadcast_message_received(message: Message, state: FSMContext):
    await state.update_data(broadcast_text=message.text)
    await state.set_state(AdminStates.broadcast_confirm)
    await message.answer(
        f"Вы собираетесь отправить следующее сообщение:\n\n---\n{message.text}\n---\n\nПодтвердите рассылку.",
        reply_markup=admin_broadcast_confirm_keyboard()
    )

@router.callback_query(F.data == "confirm_broadcast", AdminStates.broadcast_confirm)
async def broadcast_confirmed(query: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    text = data.get("broadcast_text")
    await state.clear()
    await query.message.edit_text("✅ Рассылка запущена в фоновом режиме.")
    
    # --- ИЗМЕНЕНО: Используем безопасный запуск ---
    # Рассылка сама отправляет отчет, но обертка защитит от полного падения без уведомления.
    from bot.utils.safe_task import create_safe_task
    create_safe_task(broadcast_task(bot, query.from_user.id, text), user_id=query.from_user.id, bot=bot, task_name="Рассылка")

@router.callback_query(F.data == "cancel_broadcast", AdminStates.broadcast_confirm)
async def broadcast_canceled(query: CallbackQuery, state: FSMContext):
    await state.clear()
    await query.message.edit_text("Рассылка отменена.")

# --- Выдача подписки ---
@router.message(F.text.in_({"➕ Выдать подписку", "➖ Отозвать подписку"}))
async def grant_sub_start(message: Message, state: FSMContext):
    if message.text == "➕ Выдать подписку":
        await state.update_data(sub_action='add')
        await message.answer("Введите ID пользователя для выдачи подписки. /cancel для отмены.")
    else:
        await state.update_data(sub_action='remove')
        await message.answer("Введите ID пользователя для отзыва дней подписки. /cancel для отмены.")
    await state.set_state(AdminStates.grant_sub_user_id)

@router.message(AdminStates.grant_sub_user_id)
async def grant_sub_get_id(message: Message, state: FSMContext):
    try:
        user_id = int(message.text)
        await state.update_data(target_user_id=user_id)
        await state.set_state(AdminStates.grant_sub_days)
        await message.answer(f"Введите количество дней подписки для пользователя <code>{user_id}</code>.")
    except ValueError:
        await message.reply("❌ Неверный ID. Введите число.")

@router.message(AdminStates.grant_sub_days)
async def grant_sub_get_days(message: Message, state: FSMContext, bot: Bot):
    try:
        days = int(message.text)
        if days <= 0:
            await message.reply("❌ Количество дней должно быть положительным числом.")
            return

        data = await state.get_data()
        user_id = data['target_user_id']
        action = data.get('sub_action', 'add')
        
        effective_days = days if action == 'add' else -days
        
        new_expiry_date = await db_manager.grant_subscription(user_id, effective_days)
        expiry_str = new_expiry_date.strftime('%Y-%m-%d %H:%M')
        
        if action == 'add':
            response_text = f"✅ Подписка для <code>{user_id}</code> успешно продлена на {days} дней.\nНовая дата окончания: {expiry_str}"
            notification_text = f"🎉 Ваша подписка была продлена администратором на {days} дней!\nНовая дата окончания: {expiry_str}"
        else:
            response_text = f"✅ У пользователя <code>{user_id}</code> отозвано {days} дней подписки.\nНовая дата окончания: {expiry_str}"
            notification_text = f"❗ Администратор отозвал у вас {days} дней подписки.\nНовая дата окончания: {expiry_str}"
        is_super = message.from_user.id == config.SUPER_ADMIN_ID
        await message.answer(response_text, reply_markup=admin_keyboard(is_super_admin=is_super))
        
        try:
            await bot.send_message(user_id, notification_text)
        except Exception as e:
            await message.answer(f"⚠️ Не удалось уведомить пользователя: {e}")
            
        await state.clear()
    except ValueError:
        await message.reply("❌ Неверное количество дней. Введите число.")

# --- Бан/Разбан ---
@router.message(F.text == "🚫 Бан/Разбан юзера")
async def ban_user_start(message: Message, state: FSMContext):
    await state.set_state(AdminStates.ban_user_id)
    await message.answer("Введите ID пользователя для управления баном. /cancel для отмены.")

@router.message(AdminStates.ban_user_id)
async def ban_user_get_id(message: Message, state: FSMContext):
    try:
        user_id = int(message.text)
        sub_status = await db_manager.get_subscription_status(user_id)
        is_banned = sub_status.get('is_banned', False)

        status_text = "в бане 🚫" if is_banned else "не в бане ✅"
        
        await state.set_state(AdminStates.ban_user_confirm)
        
        await message.answer(
            f"Пользователь <code>{user_id}</code> сейчас {status_text}.\nЧто вы хотите сделать?",
            reply_markup=admin_ban_confirm_keyboard(user_id, is_banned)
        )
    except ValueError:
        await message.reply("❌ Неверный ID. Введите число.")

@router.callback_query(F.data.startswith("ban_user_"), AdminStates.ban_user_confirm)
async def ban_user_confirm_callback(query: CallbackQuery, state: FSMContext, bot: Bot):
    user_id = int(query.data.split('_')[-1])
    await db_manager.set_ban_status(user_id, True)
    await query.message.edit_text(f"✅ Пользователь <code>{user_id}</code> забанен.")
    try:
        await bot.send_message(user_id, "❌ Вы были заблокированы администратором.")
    except Exception:
        pass # User might have blocked the bot
    await state.clear()

@router.callback_query(F.data.startswith("unban_user_"), AdminStates.ban_user_confirm)
async def unban_user_confirm_callback(query: CallbackQuery, state: FSMContext, bot: Bot):
    user_id = int(query.data.split('_')[-1])
    await db_manager.set_ban_status(user_id, False)
    await query.message.edit_text(f"✅ Пользователь <code>{user_id}</code> разбанен.")
    try:
        await bot.send_message(user_id, "✅ Вы были разблокированы администратором.")
    except Exception:
        pass
    await state.clear()

@router.callback_query(F.data == "cancel_ban", AdminStates.ban_user_confirm)
async def ban_user_cancel_callback(query: CallbackQuery, state: FSMContext):
    await state.clear()
    await query.message.edit_text("Действие отменено.")

# --- Промокоды ---
@router.message(F.text == "🎁 Промокоды")
async def promo_codes_menu(message: Message):
    await message.answer("Меню управления промокодами:", reply_markup=admin_promo_menu_keyboard())

@router.callback_query(F.data == "admin_create_promo")
async def create_promo_start(query: CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.create_promo_code_days)
    await query.message.edit_text("Введите количество дней подписки для нового промокода. /cancel для отмены.")

@router.message(AdminStates.create_promo_code_days)
async def create_promo_get_days(message: Message, state: FSMContext):
    try:
        days = int(message.text)
        if days <= 0:
            await message.reply("❌ Количество дней должно быть положительным числом.")
            return
        await state.update_data(promo_days=days)
        await state.set_state(AdminStates.create_promo_code_type)
        await message.answer("Выберите тип промокода:", reply_markup=admin_promo_type_keyboard())
    except ValueError:
        await message.reply("❌ Неверное количество дней. Введите число.")

@router.callback_query(F.data.startswith("promo_type_"), AdminStates.create_promo_code_type)
async def create_promo_get_type(query: CallbackQuery, state: FSMContext):
    promo_type = query.data.split('_')[-1]
    
    if promo_type == 'single':
        data = await state.get_data()
        days = data['promo_days']
        max_activations = 1
        
        promo_code = f"PROMO-{uuid.uuid4().hex[:8].upper()}"
        await db_manager.create_promo_code(promo_code, days, max_activations)
        
        await query.message.edit_text(
            f"✅ Одноразовый промокод создан!\n\n"
            f"Нажмите, чтобы скопировать: <code>{promo_code}</code>\n\n"
            f"Срок действия: {days} дней.",
            reply_markup=admin_promo_menu_keyboard()
        )
        await state.clear()
    elif promo_type == 'reusable':
        await state.set_state(AdminStates.create_promo_code_activations)
        await query.message.edit_text(
            "Введите максимальное количество активаций для многоразового промокода.\n"
            "Отправьте <b>0</b> для бесконечных активаций.\n"
            "/cancel для отмены."
        )

@router.message(AdminStates.create_promo_code_activations)
async def create_promo_get_activations(message: Message, state: FSMContext):
    try:
        max_activations = int(message.text)
        if max_activations < 0:
            await message.reply("❌ Количество активаций не может быть отрицательным.")
            return

        data = await state.get_data()
        days = data['promo_days']
        
        promo_code = f"MULTI-{uuid.uuid4().hex[:8].upper()}"
        await db_manager.create_promo_code(promo_code, days, max_activations)
        
        limit_text = "бесконечное" if max_activations == 0 else max_activations
        
        await message.answer(
            f"✅ Многоразовый промокод создан!\n\n"
            f"Нажмите, чтобы скопировать: <code>{promo_code}</code>\n\n"
            f"Срок действия: {days} дней.\n"
            f"Лимит активаций: {limit_text}.",
            reply_markup=admin_promo_menu_keyboard()
        )
        await state.clear()

    except ValueError:
        await message.reply("❌ Введите целое число.")

@router.callback_query(F.data == "admin_list_promo")
async def list_promo_codes(query: CallbackQuery):
    codes = await db_manager.get_all_promo_codes_details()
    text = "Список промокодов:"
    if not codes:
        text = "Список промокодов пуст."
    
    await query.message.edit_text(text, reply_markup=admin_promo_list_keyboard(codes))

@router.callback_query(F.data.startswith("view_promo_"))
async def view_promo_details(query: CallbackQuery, bot: Bot):
    code = query.data.split('_')[-1]
    details = await db_manager.get_promo_code_details(code)

    if not details:
        await query.answer("Промокод не найден.", show_alert=True)
        return

    max_act_text = "∞" if details['max_activations'] == 0 else details['max_activations']
    text = (f"<b>🔎 Детали промокода <code>{code}</code></b>\n\n"
            f"<b>Длительность:</b> {details['duration_days']} дн.\n"
            f"<b>Активации:</b> {details['current_activations']} / {max_act_text}\n\n"
            f"<b>Активировали:</b>\n")

    if not details['activations']:
        text += "<i>Никто еще не активировал этот код.</i>"
    else:
        user_info_list = [f"  - <code>{act['user_id']}</code> в {act['activated_at'].strftime('%Y-%m-%d %H:%M')}" for act in details['activations']]
        text += "\n".join(user_info_list)

    # Кнопка "назад" удалена по запросу. Пользователь может вернуться через основное меню.
    await query.message.edit_text(text)

@router.callback_query(F.data.startswith("admin_delete_promo_"))
async def delete_promo_code(query: CallbackQuery):
    code_to_delete = query.data.split('_')[-1]
    await db_manager.delete_promo_code(code_to_delete)
    await query.answer(f"Промокод {code_to_delete} удален.")
    codes = await db_manager.get_all_promo_codes_details()
    text = "Список промокодов:" if codes else "Список промокодов пуст."
    await query.message.edit_text(text, reply_markup=admin_promo_list_keyboard(codes))

# --- Управление админами (только для суперадмина) ---
@router.message(F.text == "👑 Управление админами", IsSuperAdminFilter())
async def manage_admins_menu(message: Message):
    admins = await db_manager.get_all_admins()
    await message.answer(
        "Меню управления администраторами.",
        reply_markup=manage_admins_keyboard(admins)
    )

@router.callback_query(F.data == "add_admin", IsSuperAdminFilter())
async def add_admin_start(query: CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.add_admin_id)
    await query.message.edit_text("Введите ID пользователя, чтобы сделать его администратором. /cancel для отмены.")
    await query.answer()

@router.message(AdminStates.add_admin_id, IsSuperAdminFilter())
async def add_admin_get_id(message: Message, state: FSMContext, bot: Bot):
    try:
        user_id = int(message.text)
        current_role = await db_manager.get_user_role(user_id)
        if current_role in ['admin', 'super_admin']:
            await message.reply("Этот пользователь уже является администратором.")
        else:
            await db_manager.set_user_role(user_id, 'admin')
            await message.reply(f"✅ Пользователь <code>{user_id}</code> назначен администратором.")
            try:
                await bot.send_message(user_id, "🎉 Поздравляем! Вы были назначены администратором.")
            except Exception:
                pass # ignore if bot is blocked
    except ValueError:
        await message.reply("❌ Неверный ID.")
    finally:
        await state.clear()
        admins = await db_manager.get_all_admins()
        await message.answer("Меню управления администраторами.", reply_markup=manage_admins_keyboard(admins))

@router.callback_query(F.data.startswith("remove_admin_"), IsSuperAdminFilter())
async def remove_admin_callback(query: CallbackQuery, bot: Bot):
    user_id_to_remove = int(query.data.split('_')[-1])
    
    if user_id_to_remove == config.SUPER_ADMIN_ID:
        await query.answer("🚫 Нельзя удалить суперадминистратора.", show_alert=True)
        return
        
    await db_manager.set_user_role(user_id_to_remove, 'user')
    await query.answer(f"Пользователь {user_id_to_remove} больше не администратор.", show_alert=True)
    
    try:
        await bot.send_message(user_id_to_remove, "❗ Вы были лишены прав администратора.")
    except Exception:
        pass
        
    admins = await db_manager.get_all_admins()
    await query.message.edit_text("Меню управления администраторами.", reply_markup=manage_admins_keyboard(admins))


# --- Shop Settings (Super Admin only) ---
@router.message(F.text == "⚙️ Настройки магазина", IsSuperAdminFilter())
async def shop_settings_menu(message: Message):
    await message.answer(
        "Настройки кнопок в магазине:",
        reply_markup=await admin_shop_settings_keyboard()
    )

@router.callback_query(F.data == "admin_set_support_contact", IsSuperAdminFilter())
async def set_support_contact_start(query: CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.set_support_contact)
    await query.message.edit_text(
        "Введите новый контакт для кнопок покупки (юзернейм @username или ссылка https://...).\n"
        "/cancel для отмены."
    )
    await query.answer()

@router.message(AdminStates.set_support_contact, IsSuperAdminFilter())
async def set_support_contact_received(message: Message, state: FSMContext):
    contact = message.text.strip()
    await db_manager.set_bot_setting('support_contact', contact)
    await state.clear()
    await message.answer(
        f"✅ Контакт для покупки обновлен.\nНовое значение: {contact}",
        reply_markup=await admin_shop_settings_keyboard()
    )

@router.callback_query(F.data == "admin_toggle_buy_sessions", IsSuperAdminFilter())
async def toggle_buy_sessions_button(query: CallbackQuery):
    current_val = await db_manager.get_bot_setting('show_buy_sessions_button')
    new_val = '0' if current_val != '0' else '1'
    await db_manager.set_bot_setting('show_buy_sessions_button', new_val)
    await query.message.edit_reply_markup(reply_markup=await admin_shop_settings_keyboard())
    status_text = "включена" if new_val != '0' else "выключена"
    await query.answer(f"Кнопка 'Купить сессии' {status_text}")

# --- Тех. работы (только для суперадмина) ---
@router.message(F.text == "🛠️ Тех. работы", IsSuperAdminFilter())
async def toggle_maintenance_mode(message: Message):
    current_status_str = await db_manager.get_bot_setting("maintenance")
    current_status = current_status_str == "1"
    new_status = not current_status
    await db_manager.set_bot_setting("maintenance", "1" if new_status else "0")
    if new_status:
        notification_text = "🛠️ <b>Включен режим технических работ.</b>\n\nОбычные пользователи не смогут пользоваться ботом."
    else:
        notification_text = "✅ <b>Режим технических работ выключен.</b>\n\nБот снова доступен для всех пользователей."
    await message.answer(notification_text)

# --- Перезагрузка бота (только для суперадмина) ---
@router.message(F.text == "🔄 Перезагрузка бота", IsSuperAdminFilter())
async def restart_bot_start(message: Message, state: FSMContext):
    await state.set_state(AdminStates.restart_confirm)
    await message.answer(
        "Вы уверены, что хотите перезагрузить бота?\n"
        "Все активные задачи будут остановлены.",
        reply_markup=admin_restart_confirm_keyboard()
    )

@router.callback_query(F.data == "confirm_restart", AdminStates.restart_confirm)
async def restart_bot_confirmed(query: CallbackQuery, state: FSMContext, bot: Bot, restart_function: Callable):
    await state.clear()
    await query.message.edit_text("✅ Перезагрузка начата. Бот скоро вернется в строй.")
    # Запускаем перезагрузку в фоновой задаче, чтобы успеть ответить на коллбэк
    asyncio.create_task(restart_function(bot))

@router.callback_query(F.data == "cancel_restart", AdminStates.restart_confirm)
async def restart_bot_canceled(query: CallbackQuery, state: FSMContext):
    await state.clear()
    await query.message.edit_text("Перезагрузка отменена.")

# --- Информация о пользователе ---
@router.message(F.text == "🔍 Информация о юзере")
async def user_info_start(message: Message, state: FSMContext):
    await state.set_state(AdminStates.user_info_id)
    await message.answer("Введите ID пользователя для получения информации. /cancel для отмены.")

@router.message(AdminStates.user_info_id)
async def get_user_info(message: Message, state: FSMContext, bot: Bot):
    try:
        user_id = int(message.text)
        await state.clear()

        # Get user info from Telegram
        try:
            user_chat = await bot.get_chat(user_id)
            username = f"@{user_chat.username}" if user_chat.username else "нет"
            full_name = html.escape(user_chat.full_name)
            user_info_text = (
                f"<b>Информация из Telegram</b>\n"
                f"▫️ Full Name: {full_name}\n"
                f"▫️ Username: {username}\n"
            )
        except Exception:
            user_info_text = "<b>Информация из Telegram</b>\n▫️ Не удалось получить данные о пользователе (возможно, не начинал диалог с ботом)."

        # Get subscription info
        sub_status = await db_manager.get_subscription_status(user_id)
        if sub_status['active']:
            expires_at_str = sub_status['expires_at'].strftime('%Y-%m-%d %H:%M')
            sub_text = f"Активна до {expires_at_str} ✅"
        else:
            sub_text = "Неактивна ❌"

        ban_text = "Да 🚫" if sub_status.get('is_banned') else "Нет ✅"

        # Get bot data
        user_data = await db_manager.get_user_data(user_id)
        chats_count = await db_manager.get_chats_count(user_id)
        comments = await db_manager.get_comments(user_id)
        delay = await db_manager.get_delay(user_id)

        bot_data_text = (
            f"\n<b>Данные в боте</b>\n"
            f"▫️ Подписка: {sub_text}\n"
            f"▫️ Заблокирован: {ban_text}\n"
            f"▫️ Сессий: {len(user_data['sessions'])}\n"
            f"▫️ Групп: {chats_count}\n"
            f"▫️ Прокси: {len(user_data['proxies'])}\n"
            f"▫️ Текстов: {len(comments)}\n"
            f"▫️ Задержка: {delay} сек."
        )

        is_super = message.from_user.id == config.SUPER_ADMIN_ID
        await message.answer(f"<b>🔎 Сводка по пользователю <code>{user_id}</code></b>\n\n{user_info_text}{bot_data_text}", reply_markup=admin_keyboard(is_super_admin=is_super))

    except ValueError:
        await message.reply("❌ Неверный ID. Введите число.")