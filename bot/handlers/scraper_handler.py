
import logging
import os

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Document, FSInputFile, Message

from bot.client_tasks.scraper import scraper_task
from bot.database.db_manager import db_manager
from bot.keyboards import scraper_menu_keyboard, settings_keyboard
from bot.middlewares import check_subscription
from bot.states import ScraperStates
from bot.utils.safe_task import create_safe_task

router = Router()
logger = logging.getLogger(__name__)


@router.message(F.text == "👤 Сбор аудитории")
async def scraper_menu(message: Message):
    user_id = message.from_user.id
    scraped_count = await db_manager.get_scraped_users_count(user_id)
    settings = await db_manager.get_ai_settings(user_id)
    filter_level = settings.get("user_activity_filter", "all")
    await message.answer(
        "Меню сбора аудитории из групп.",
        reply_markup=scraper_menu_keyboard(scraped_count, filter_level)
    )


@router.callback_query(F.data == "scraper_toggle_filter")
async def toggle_user_activity_filter_callback(query: CallbackQuery):
    """Переключает фильтр сбора аудитории."""
    if not await check_subscription(query):
        return
    user_id = query.from_user.id

    current_settings = await db_manager.get_ai_settings(user_id)
    current_filter = current_settings.get("user_activity_filter", "all")

    filter_cycle = ["all", "recent", "week"]
    try:
        current_index = filter_cycle.index(current_filter)
        new_filter = filter_cycle[(current_index + 1) % len(filter_cycle)]
    except ValueError:
        new_filter = "all"

    await db_manager.set_user_activity_filter(user_id, new_filter)

    # Обновляем меню
    scraped_count = await db_manager.get_scraped_users_count(user_id)
    markup = scraper_menu_keyboard(scraped_count, new_filter)
    await query.message.edit_reply_markup(reply_markup=markup)
    await query.answer(f"Фильтр изменен на: {new_filter}")


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


@router.callback_query(F.data == "scraper_export")
async def export_scraped_users(query: CallbackQuery):
    user_id = query.from_user.id
    await query.answer("Готовлю файл для выгрузки...")

    count = await db_manager.get_scraped_users_count(user_id)
    if count == 0:
        await query.message.answer("База пуста, нечего выгружать.")
        return

    file_path = f"scraped_users_{user_id}.txt"
    try:
        written_count = 0
        with open(file_path, "w") as f:
            async for scraped_user_id in db_manager.get_scraped_users_stream(user_id):
                f.write(f"{scraped_user_id}\n")
                written_count += 1

        await query.message.answer_document(
            document=FSInputFile(file_path),
            caption=f"✅ Ваша база пользователей ({written_count} ID) готова."
        )
    except Exception as e:
        logger.error(f"Failed to export scraped users for {user_id}: {e}", exc_info=True)
        await query.message.answer(f"❌ Произошла ошибка при выгрузке: {e}")
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)


@router.callback_query(F.data == "scraper_import")
async def import_scraped_users_start(query: CallbackQuery, state: FSMContext):
    if not await check_subscription(query):
        return
    await state.set_state(ScraperStates.import_users_file)
    await query.message.edit_text(
        "📥 Отправьте файл .txt со списком ID пользователей для импорта.\n"
        "Каждый ID должен быть на новой строке.\n\n"
        "/cancel для отмены."
    )
    await query.answer()


@router.message(ScraperStates.import_users_file, F.document)
async def import_scraped_users_file_received(message: Message, state: FSMContext):
    document = message.document
    if not document.file_name.lower().endswith('.txt'):
        await message.reply("❌ Поддерживаются только файлы .txt.")
        return

    await message.answer("⏳ Обрабатываю файл, это может занять некоторое время...")

    try:
        file_info = await message.bot.get_file(document.file_id)
        file_content_bytes = await message.bot.download_file(file_info.file_path)
        file_content = file_content_bytes.read().decode('utf-8')

        user_ids = [int(line.strip()) for line in file_content.splitlines() if line.strip().isdigit()]

        if not user_ids:
            await message.reply("❌ В файле не найдено валидных ID пользователей.")
            await state.clear()
            return

        user_id = message.from_user.id
        added_count = await db_manager.import_scraped_users(user_id, user_ids)
        total_count = await db_manager.get_scraped_users_count(user_id)

        await message.answer(
            f"✅ Импорт завершен!\n\n"
            f"▫️ Добавлено новых уникальных пользователей: {added_count}\n"
            f"▫️ Всего в вашей базе: {total_count}",
            reply_markup=settings_keyboard()
        )
        await state.clear()

    except Exception as e:
        logger.error(f"Error processing imported users file for user {message.from_user.id}: {e}", exc_info=True)
        await message.reply(f"❌ Произошла ошибка при чтении файла: {e}")
        await state.clear()


@router.callback_query(F.data == "scraper_clear_all")
async def clear_scraped_users(query: CallbackQuery):
    # --- ДОБАВЛЕНО: Проверка подписки ---
    if not await check_subscription(query):
        return
    user_id = query.from_user.id
    await db_manager.reset_scraped_users(user_id)
    await query.answer("✅ База собранных пользователей очищена.", show_alert=True)
    
    # Обновляем меню, чтобы показать 0 и текущий фильтр
    settings = await db_manager.get_ai_settings(user_id)
    filter_level = settings.get("user_activity_filter", "all")
    await query.message.edit_reply_markup(reply_markup=scraper_menu_keyboard(0, filter_level))