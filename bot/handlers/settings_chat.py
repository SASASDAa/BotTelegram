# bot/handlers/settings_chat.py
import html
import re

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

import config
from bot.client_tasks.search import search_chats_task
from bot.database.db_manager import db_manager
from bot.middlewares import check_subscription
from bot.states import ChatStates, SearchStates
from bot.utils.safe_task import create_safe_task
from ..keyboards import chats_keyboard_markup, settings_keyboard

router = Router()

async def show_chats_page(message: Message, user_id: int, page: int = 1):
    total_chats = await db_manager.get_chats_count(user_id)
    total_pages = (total_chats + config.CHATS_PER_PAGE - 1) // config.CHATS_PER_PAGE or 1
    page = max(1, min(page, total_pages))

    chats_on_page = await db_manager.get_paginated_chats(user_id, page, config.CHATS_PER_PAGE)

    text = f"<b>📢 Ваши группы (Страница {page}/{total_pages}):</b>\n"
    if not total_chats:
        text = "<b>📢 Ваши группы для сообщений:</b>\nСписок пуст."
    else:
        # No need to list them all if the keyboard shows them
        pass

    markup = chats_keyboard_markup(chats_on_page, page, total_pages)
    await message.edit_text(text, reply_markup=markup)


@router.message(F.text == "📢 Группы")
async def manage_chats_command(message: Message):
    # Send a temporary message that we can then edit
    sent_message = await message.answer("Загружаю список групп...")
    await show_chats_page(sent_message, message.from_user.id, page=1)


@router.callback_query(F.data.startswith("chats_page_"))
async def chats_page_callback(query: CallbackQuery):
    await query.answer()
    page = int(query.data.split('_')[-1])
    await show_chats_page(query.message, query.from_user.id, page=page)


@router.callback_query(F.data == "add_chats_list")
async def add_chats_start(query: CallbackQuery, state: FSMContext):
    # --- ДОБАВЛЕНО: Проверка подписки ---
    if not await check_subscription(query):
        return
    await query.answer()
    await query.message.edit_text(
        "📢 Введите юзернеймы (@group), публичные ссылки (t.me/group) или joinchat-ссылки групп, "
        "через запятую или каждый с новой строки.\nПример: @mygroup, t.me/anothergroup, t.me/joinchat/XXXXXX\n"
        "Отправьте /cancel для отмены."
    )
    await state.set_state(ChatStates.add_list)


@router.message(ChatStates.add_list)
async def add_chats_received(message: Message, state: FSMContext):
    raw_chats = re.split(r'[,\n]', message.text)
    
    chats_to_add = []
    for chat_input in raw_chats:
        clean_input = chat_input.strip()
        if not clean_input:
            continue

        # Нормализуем публичные ссылки (https://t.me/username) в формат @username
        # Pyrogram хорошо обрабатывает @username и t.me/joinchat/XXXX, но полные URL могут вызывать UsernameInvalid.
        if clean_input.startswith(('http://t.me/', 'https://t.me/')):
            path_part = clean_input.split('t.me/')[1]
            if not path_part.startswith(('joinchat', '+')):
                # Это ссылка на публичный чат/канал
                chats_to_add.append('@' + path_part.split('/')[0])
                continue
        
        chats_to_add.append(clean_input)

    if not chats_to_add:
        await message.reply("❌ Группы не введены. Попробуйте снова или /cancel.")
        return

    user_id = message.from_user.id
    await db_manager.add_chats(user_id, chats_to_add)
    await state.clear()

    # Отправляем новое сообщение и сразу его редактируем, чтобы показать обновленный список
    sent_message = await message.answer(f"✅ Группы ({len(chats_to_add)}) добавлены! Обновляю список...")
    await show_chats_page(sent_message, user_id, page=1)
@router.callback_query(F.data.startswith("delete_chat_"))
async def delete_chat_callback(query: CallbackQuery):
    # --- ДОБАВЛЕНО: Проверка подписки ---
    if not await check_subscription(query):
        return
    chat_to_delete = query.data.replace("delete_chat_", "", 1)

    current_page = 1
    if query.message and query.message.text:
        match = re.search(r'Страница (\d+)', query.message.text)
        if match:
            current_page = int(match.group(1))

    await query.answer(f"Удаляю чат...")
    await db_manager.delete_chat(query.from_user.id, chat_to_delete)
    await show_chats_page(query.message, query.from_user.id, page=current_page)


# --- Поиск групп ---
@router.callback_query(F.data == "find_chats")
async def find_chats_start(query: CallbackQuery, state: FSMContext):
    # --- ДОБАВЛЕНО: Проверка подписки ---
    if not await check_subscription(query):
        return
    await query.answer()
    await query.message.edit_text(
        "🔍 Введите ключевые слова для поиска ГРУПП (через запятую).\n"
        "Отправьте /cancel для отмены."
    )
    await state.set_state(SearchStates.enter_keywords)

@router.message(SearchStates.enter_keywords)
async def process_keywords_for_search_chats(message: Message, state: FSMContext):
    keywords = [k.strip() for k in message.text.split(',') if k.strip()]
    if not keywords:
        await message.reply("❌ Нет ключевых слов. Попробуйте снова или /cancel.")
        return

    user_id = message.from_user.id
    await message.reply("🔎 Начинаю поиск ГРУПП... Это может занять время. Вы получите отчет по завершении.",
                        reply_markup=settings_keyboard())
    await state.clear()
    # --- ИЗМЕНЕНО: Используем безопасный запуск ---
    create_safe_task(search_chats_task(
        bot=message.bot,
        user_id=message.from_user.id,
        keywords=keywords
    ), user_id=message.from_user.id, bot=message.bot, task_name="Поиск групп")