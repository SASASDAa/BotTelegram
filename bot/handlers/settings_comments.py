# bot/handlers/settings_comments.py
import html
import logging
import os

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Document, Message

from bot.database.db_manager import db_manager
from bot.middlewares import check_subscription
from bot.keyboards import comments_menu_keyboard, settings_keyboard
from bot.states import CommentStates

router = Router()
logger = logging.getLogger(__name__)

@router.message(F.text == "✏️ Тексты")
async def manage_comments_command(message: Message, state: FSMContext):
    user_id = message.from_user.id
    comments = await db_manager.get_comments(user_id)
    photo_path = await db_manager.get_spam_photo(user_id)

    text = "<b>💬 Настройка текстов и медиа для спама</b>\n\n"
    
    text += "<b>Текущие тексты:</b>\n"
    if comments:
        # Show only first 5 comments for brevity
        display_comments = comments[:5]
        text += "\n".join([f"• {html.escape(c[:80])}{'...' if len(c) > 80 else ''}" for c in display_comments])
        if len(comments) > 5:
            text += f"\n... и еще {len(comments) - 5}"
    else:
        text += "<i>Не установлены.</i>"

    text += "\n\n<b>Текущее фото:</b>\n"
    if photo_path:
        text += "<i>Прикреплено ✅</i>"
    else:
        text += "<i>Отсутствует ❌</i>"
    
    text += "\n\nИспользуйте кнопки ниже для управления."

    await message.answer(text, reply_markup=comments_menu_keyboard(has_photo=bool(photo_path)))

@router.callback_query(F.data == "edit_spam_texts")
async def edit_spam_texts_start(query: CallbackQuery, state: FSMContext):
    # --- ДОБАВЛЕНО: Проверка подписки ---
    if not await check_subscription(query):
        return
    await query.answer()
    await query.message.edit_text(
        "Отправьте новый текст (или несколько через запятую для случайного выбора).\n\n"
        "Или <b>отправьте файл .txt/.html</b>, чтобы использовать его содержимое как один большой текст для спама.\n\n"
        "Отправьте /cancel для отмены."
    )
    await state.set_state(CommentStates.enter_text)

@router.callback_query(F.data == "add_spam_photo")
async def add_spam_photo_start(query: CallbackQuery, state: FSMContext):
    # --- ДОБАВЛЕНО: Проверка подписки ---
    if not await check_subscription(query):
        return
    await query.answer()
    await query.message.edit_text("Отправьте фото, которое будет прикрепляться к сообщениям.\n/cancel для отмены.")
    await state.set_state(CommentStates.add_photo)

@router.callback_query(F.data == "delete_spam_photo")
async def delete_spam_photo_handler(query: CallbackQuery):
    # --- ДОБАВЛЕНО: Проверка подписки ---
    if not await check_subscription(query):
        return
    user_id = query.from_user.id
    await db_manager.delete_spam_photo(user_id)
    await query.answer("✅ Фото удалено.", show_alert=True)
    # Refresh the menu by editing the message
    await query.message.edit_reply_markup(reply_markup=comments_menu_keyboard(has_photo=False))

@router.message(CommentStates.enter_text, F.text)
async def save_comments_from_text(message: Message, state: FSMContext):
    comment_texts = [t.strip() for t in message.text.split(',') if t.strip()]
    if not comment_texts:
        await message.reply("❌ Текст сообщения не может быть пустым. Попробуйте снова или /cancel.")
        return

    user_id = message.from_user.id
    await db_manager.update_comments(user_id, comment_texts)
    await message.reply(
        f"✅ Тексты сообщений обновлены! Всего: {len(comment_texts)}.",
        reply_markup=settings_keyboard()
    )
    await state.clear()

@router.message(CommentStates.enter_text, F.document)
async def save_comments_from_file(message: Message, state: FSMContext, bot: Bot):
    document = message.document
    if not document.file_name.lower().endswith(('.txt', '.html')):
        await message.reply("❌ Поддерживаются только файлы .txt и .html.")
        return

    try:
        file_info = await bot.get_file(document.file_id)
        file_content_bytes = await bot.download_file(file_info.file_path)
        file_content = file_content_bytes.read().decode('utf-8')

        if not file_content.strip():
            await message.reply("❌ Файл пуст.")
            return

        # Save content as a single comment
        await db_manager.update_comments(message.from_user.id, [file_content])
        await message.reply(
            f"✅ Текст из файла <code>{html.escape(document.file_name)}</code> успешно загружен.",
            reply_markup=settings_keyboard()
        )
        await state.clear()
    except Exception as e:
        logger.error(f"Error processing comment file for user {message.from_user.id}: {e}", exc_info=True)
        await message.reply(f"❌ Произошла ошибка при чтении файла: {e}")


@router.message(CommentStates.add_photo, F.photo)
async def save_spam_photo(message: Message, state: FSMContext, bot: Bot):
    user_id = message.from_user.id
    photo_file_id = message.photo[-1].file_id # Get the highest resolution

    media_dir = os.path.abspath(f"media/{user_id}")
    os.makedirs(media_dir, exist_ok=True)
    photo_path = os.path.join(media_dir, "spam_photo.jpg")

    try:
        await bot.download(file=photo_file_id, destination=photo_path)
        await db_manager.set_spam_photo(user_id, photo_path)
        await message.reply(
            "✅ Фото для спама сохранено.",
            reply_markup=settings_keyboard()
        )
    except Exception as e:
        logger.error(f"Error saving spam photo for user {user_id}: {e}", exc_info=True)
        await message.reply(f"❌ Не удалось сохранить фото. Ошибка: {e}")
    finally:
        await state.clear()