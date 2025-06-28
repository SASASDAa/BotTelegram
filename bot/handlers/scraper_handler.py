
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.client_tasks.scraper import scraper_task
from bot.database.db_manager import db_manager
from bot.keyboards import scraper_menu_keyboard, settings_keyboard
from bot.middlewares import check_subscription
from bot.states import ScraperStates
from bot.utils.safe_task import create_safe_task

router = Router()


@router.message(F.text == "👤 Сбор аудитории")
async def scraper_menu(message: Message):
    user_id = message.from_user.id
    count = await db_manager.get_scraped_users_count(user_id)
    await message.answer(
        "Меню сбора аудитории из групп.",
        reply_markup=scraper_menu_keyboard(count)
    )


@router.callback_query(F.data == "scraper_start_new")
async def start_new_scrape(query: CallbackQuery, state: FSMContext):
    # --- ДОБАВЛЕНО: Проверка подписки ---
    if not await check_subscription(query):
        return
    await state.set_state(ScraperStates.enter_group)
    await query.message.edit_text(
        "Введите юзернейм или ссылку на публичную группу для сбора участников.\n"
        "Пример: @durov_russia или https://t.me/tgram\n\n"
        "/cancel для отмены."
    )
    await query.answer()


@router.message(ScraperStates.enter_group)
async def group_to_scrape_received(message: Message, state: FSMContext):
    target_group_input = message.text.strip()

    # Нормализуем ввод, чтобы Pyrogram правильно обработал полные ссылки
    target_group = target_group_input
    if target_group.startswith(('http://t.me/', 'https://t.me/')):
        path_part = target_group.split('t.me/')[1]
        # Нас интересуют только публичные группы по юзернейму для сбора
        if not path_part.startswith(('joinchat', '+')):
            # Pyrogram хорошо обрабатывает @username
            target_group = '@' + path_part.split('/')[0]

    await state.clear()
    await message.answer(
        f"▶️ Запускаю сбор участников из <code>{target_group_input}</code>. "
        "Это может занять некоторое время. Вы получите отчет по завершении.",
        reply_markup=settings_keyboard()
    )
    # --- ИЗМЕНЕНО: Используем безопасный запуск ---
    create_safe_task(scraper_task(message.bot, message.from_user.id, target_group), user_id=message.from_user.id, bot=message.bot, task_name="Сбор аудитории")


@router.callback_query(F.data == "scraper_clear_all")
async def clear_scraped_users(query: CallbackQuery):
    # --- ДОБАВЛЕНО: Проверка подписки ---
    if not await check_subscription(query):
        return
    user_id = query.from_user.id
    await db_manager.reset_scraped_users(user_id)
    await query.answer("✅ База собранных пользователей очищена.", show_alert=True)
    count = await db_manager.get_scraped_users_count(user_id)
    await query.message.edit_reply_markup(reply_markup=scraper_menu_keyboard(count))