# bot/handlers/settings_proxy.py
import re

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

import config
from bot.client_tasks.client_manager import ATTACK_STATUS, SPAM_STATUS
from bot.middlewares import check_subscription
from bot.database.db_manager import db_manager
from bot.keyboards import proxies_keyboard_markup, settings_keyboard
from bot.states import ProxyStates
from bot.utils.proxy_parser import parse_proxy_string

router = Router()

async def show_proxies_page(message: Message, user_id: int, page: int = 1):
    proxies_list = await db_manager.get_proxies(user_id)
    ai_settings = await db_manager.get_ai_settings(user_id)
    use_proxy = ai_settings.get("use_proxy", True)
    total_pages = (len(proxies_list) + config.PROXIES_PER_PAGE - 1) // config.PROXIES_PER_PAGE or 1
    page = max(1, min(page, total_pages))

    start_index = (page - 1) * config.PROXIES_PER_PAGE
    end_index = start_index + config.PROXIES_PER_PAGE
    proxies_on_page = proxies_list[start_index:end_index]

    text = f"<b>🌐 Ваши прокси (Страница {page}/{total_pages}):</b>\n"
    if not proxies_list:
        text = "<b>🌐 Ваши прокси:</b>\nСписок пуст."

    markup = proxies_keyboard_markup(proxies_on_page, page, total_pages, use_proxy)
    await message.edit_text(text, reply_markup=markup)


@router.message(F.text == "🌐 Прокси")
async def manage_proxies_command(message: Message):
    sent_message = await message.answer("Загружаю список прокси...")
    await show_proxies_page(sent_message, message.from_user.id, page=1)


@router.callback_query(F.data.startswith("proxies_page_"))
async def proxies_page_callback(query: CallbackQuery):
    await query.answer()
    page = int(query.data.split('_')[-1])
    await show_proxies_page(query.message, query.from_user.id, page=page)


@router.callback_query(F.data == "add_proxy")
async def add_proxy_start(query: CallbackQuery, state: FSMContext):
    # --- ДОБАВЛЕНО: Проверка подписки ---
    if not await check_subscription(query):
        return
    await query.answer()
    await query.message.edit_text(
        "🌐 Введите прокси в формате `protocol://user:pass@host:port` или `protocol://host:port`.\n"
        "Поддерживаемые протоколы: http, socks4, socks5.\n"
        "Отправьте /cancel для отмены."
    )
    await state.set_state(ProxyStates.add_proxy)


@router.message(ProxyStates.add_proxy)
async def add_proxy_received(message: Message, state: FSMContext):
    proxy_string = message.text.strip()
    if not parse_proxy_string(proxy_string):
        await message.reply("❌ Неверный формат прокси. Попробуйте снова или /cancel.")
        return
    user_id = message.from_user.id
    await db_manager.add_proxy(user_id, proxy_string)
    await message.reply("✅ Прокси добавлен!", reply_markup=settings_keyboard())
    await state.clear()


@router.callback_query(F.data.startswith("delete_proxy_"))
async def delete_proxy_callback(query: CallbackQuery):
    # --- ДОБАВЛЕНО: Проверка подписки ---
    if not await check_subscription(query):
        return
    proxy_to_delete = query.data.replace("delete_proxy_", "", 1)

    current_page = 1
    if query.message and query.message.text:
        match = re.search(r'Страница (\d+)', query.message.text)
        if match:
            current_page = int(match.group(1))

    await query.answer("Удаляю прокси...")
    await db_manager.delete_proxy(query.from_user.id, proxy_to_delete)
    await show_proxies_page(query.message, query.from_user.id, page=current_page)


@router.callback_query(F.data == "toggle_proxy_usage")
async def toggle_proxy_usage_callback(query: CallbackQuery):
    # --- ДОБАВЛЕНО: Проверка подписки ---
    if not await check_subscription(query):
        return
    user_id = query.from_user.id
    await query.answer()

    current_settings = await db_manager.get_ai_settings(user_id)
    new_status = not current_settings.get("use_proxy", True)
    await db_manager.set_proxy_enabled(user_id, new_status)

    # Refresh the menu. Assume page 1 is fine for a refresh after a toggle.
    await show_proxies_page(query.message, user_id, page=1)