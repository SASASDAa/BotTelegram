# bot/handlers/settings_sessions.py
import asyncio
import html
import logging
import os
import re

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, InlineKeyboardButton, InlineKeyboardMarkup
from pyrogram import Client
# Pyrogram imports for session handling
from pyrogram.errors import (
    AuthKeyUnregistered, FloodWait, PhoneCodeExpired, PhoneCodeInvalid,
    PhoneNumberInvalid, SessionPasswordNeeded, UserDeactivated
)
from telethon import TelegramClient
from telethon.errors import (
    SessionPasswordNeededError, PhoneCodeInvalidError, FloodWaitError,
    PhoneNumberBannedError, AuthKeyUnregisteredError, UserDeactivatedError
)
from telethon.sessions import StringSession

from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.filters import Command
import config
# --- ИЗМЕНЕНО: Импортируем кэш и его лок для инвалидации ---
from bot.client_tasks.client_manager import (
    validate_user_sessions, SESSION_VALIDATION_CACHE, SESSION_VALIDATION_CACHE_LOCK, FSM_TELETHON_CLIENTS
)
from bot.database.db_manager import db_manager
from bot.keyboards import sessions_keyboard_markup, settings_keyboard, main_keyboard, select_client_type_keyboard
from bot.middlewares import check_subscription
from bot.states import SessionStates

class FSMClient(Client):
    """
    A Pyrogram Client subclass that prevents the default interactive
    authorization flow (console prompts). This is intended for use with
    an FSM-based authorization flow, where the bot asks the user for
    details in the chat.
    """
    async def authorize(self):
        # Override the default authorize method to do nothing.
        # The FSM handlers will call send_code, sign_in, etc., manually.
        pass

router = Router()
logger = logging.getLogger(__name__)
FSM_CLIENTS = {}  # Global dict to hold active clients during FSM flow
CLIENT_TYPE_HELP_TEXT = (
    "<b>В чем разница между Pyrogram и Telethon?</b>\n\n"
    "Это две разные библиотеки для работы с Telegram API. Они имеют разные форматы файлов сессий и несовместимы друг с другом.\n\n"
    "🔹 <b>Pyrogram (Стабильный)</b>\n"
    "Рекомендуется для большинства задач: спам в группы, парсинг, прогрев. Сессии этого типа проходят проверку на валидность в меню.\n\n"
    "🔸 <b>Telethon (Для атак)</b>\n"
    "Используется <b>исключительно для модуля 'Атака в ЛС'</b>. Считается, что эта библиотека предоставляет больше низкоуровневых возможностей, но также может нести <b>повышенный риск блокировки аккаунта</b> при агрессивном использовании. Сессии Telethon намеренно <b>не проходят автоматическую проверку на валидность</b>, чтобы снизить частоту обращений к API и уменьшить риск бана."
)

@router.message(Command("cancel"), SessionStates.adding_phone, SessionStates.adding_code, SessionStates.adding_password, SessionStates.adding_phone_telethon, SessionStates.adding_code_telethon, SessionStates.adding_password_telethon)
async def cancel_session_add(message: Message, state: FSMContext):
    """Handles cancellation during the session adding process."""
    user_id = message.from_user.id
    client = FSM_CLIENTS.pop(user_id, None)
    if client and client.is_connected:
        await client.disconnect()

    telethon_client = FSM_TELETHON_CLIENTS.pop(user_id, None)
    if telethon_client and telethon_client.is_connected():
        await telethon_client.disconnect()

    await state.clear()
    await message.answer("Действие отменено.", reply_markup=settings_keyboard())

def _get_client_type_from_message(message: Message) -> str | None:
    """Извлекает тип клиента ('pyrogram' или 'telethon') из текста сообщения."""
    if not message or not message.text:
        return None
    if 'Pyrogram' in message.text:
        return 'pyrogram'
    if 'Telethon' in message.text:
        return 'telethon'
    return None

async def show_sessions_menu(message: Message, user_id: int, client_type: str, page: int = 1):
    """Отправляет или редактирует сообщение с меню сессий."""
    type_map = {'pyrogram': 'Pyrogram', 'telethon': 'Telethon'}
    display_type = type_map.get(client_type, client_type.capitalize())

    await message.edit_text(f"🔄 Проверяю {display_type} сессии, пожалуйста, подождите...")
    try:
        # --- ИЗМЕНЕНО: Надежная логика получения списка сессий ---
        # 1. Сначала получаем из БД все сессии нужного типа. Это гарантирует, что никто не потеряется.
        sessions_from_db = await db_manager.get_sessions_by_type(user_id, client_type)

        # 2. Получаем статусы валидации для ВСЕХ сессий и превращаем в словарь для быстрого доступа.
        all_validated_statuses = await validate_user_sessions(user_id)
        status_map = {s.get('phone'): s for s in all_validated_statuses}

        # 3. Собираем итоговый список, обогащая данные из БД статусами валидации.
        session_statuses = []
        for phone, path in sessions_from_db.items():
            status_info = status_map.get(phone, {})
            session_statuses.append({
                'phone': phone,
                'status': status_info.get('status', '❓ Проверка...'),
                'is_bad': status_info.get('is_bad', True),
                'type': client_type
            })

        sorted_statuses = sorted(session_statuses, key=lambda x: x.get('is_bad', False), reverse=True)

        total_pages = (len(sorted_statuses) + config.SESSIONS_PER_PAGE - 1) // config.SESSIONS_PER_PAGE or 1
        page = max(1, min(page, total_pages))

        start_index = (page - 1) * config.SESSIONS_PER_PAGE
        end_index = start_index + config.SESSIONS_PER_PAGE
        sessions_on_page = sorted_statuses[start_index:end_index]

        text = f"<b>📱 Ваши {display_type} аккаунты (Страница {page}/{total_pages}):</b>\n\n"
        if not session_statuses:
            text += "Список аккаунтов этого типа пуст."
        else:
            text += "Нажмите на аккаунт, чтобы удалить его. [P] - Pyrogram, [T] - Telethon."

        markup = sessions_keyboard_markup(sessions_on_page, page, total_pages, client_type)
        # --- ИЗМЕНЕНО: Добавляем кнопку "Назад" к сгенерированной клавиатуре ---
        markup.inline_keyboard.append(
            [InlineKeyboardButton(text="🔙 К выбору типа", callback_data="back_to_session_type_selection")]
        )

        await message.edit_text(text, reply_markup=markup)
    except Exception as e:
        logger.error(f"Error in show_sessions_menu for user {user_id}: {e}", exc_info=True)
        await message.edit_text("❌ Произошла ошибка при проверке сессий. Попробуйте позже.", reply_markup=main_keyboard())

def _get_session_type_selection_keyboard() -> InlineKeyboardMarkup:
    """Генерирует клавиатуру для выбора типа сессии."""
    builder = InlineKeyboardBuilder()
    builder.button(text="🔹 Pyrogram сессии", callback_data="list_sessions_pyrogram")
    builder.button(text="🔸 Telethon сессии", callback_data="list_sessions_telethon")
    builder.button(text="В чем разница?", callback_data="client_type_help_from_menu")
    builder.adjust(1)
    return builder.as_markup()

@router.message(F.text == "📱 Сессии")
async def manage_sessions_command(message: Message):
    await message.answer(
        "Выберите тип сессий для просмотра:",
        reply_markup=_get_session_type_selection_keyboard()
    )

@router.callback_query(F.data.startswith("list_sessions_"))
async def list_sessions_by_type_callback(query: CallbackQuery):
    client_type = query.data.split('_')[-1] # pyrogram or telethon
    await query.answer()
    await show_sessions_menu(query.message, query.from_user.id, client_type=client_type, page=1)

@router.callback_query(F.data == "back_to_session_type_selection")
async def back_to_session_type_selection_callback(query: CallbackQuery):
    await query.answer()
    await query.message.edit_text(
        "Выберите тип сессий для просмотра:",
        reply_markup=_get_session_type_selection_keyboard()
    )

@router.callback_query(F.data == "client_type_help_from_menu")
async def client_type_help_from_menu_callback(query: CallbackQuery):
    """Показывает справку о типах клиентов из главного меню выбора."""
    await query.answer()
    builder = InlineKeyboardBuilder()
    builder.button(text="🔙 Назад", callback_data="back_to_session_type_selection")

    await query.message.edit_text(
        CLIENT_TYPE_HELP_TEXT,
        reply_markup=builder.as_markup()
    )

@router.callback_query(F.data.startswith("sessions_page_"))
async def sessions_page_callback(query: CallbackQuery):
    await query.answer()
    client_type = _get_client_type_from_message(query.message)
    if not client_type:
        await query.answer("Ошибка: не удалось определить тип сессий для пагинации.", show_alert=True)
        logger.warning(f"Could not determine client_type for pagination for user {query.from_user.id}")
        return

    page = int(query.data.split('_')[-1])
    await show_sessions_menu(query.message, query.from_user.id, client_type=client_type, page=page)

@router.callback_query(F.data == "refresh_sessions")
async def refresh_sessions_callback(query: CallbackQuery):
    user_id = query.from_user.id
    client_type = _get_client_type_from_message(query.message)
    if not client_type:
        await query.answer("Ошибка: не удалось определить тип сессий для обновления.", show_alert=True)
        logger.warning(f"Could not determine client_type for refresh for user {user_id}")
        return

    async with SESSION_VALIDATION_CACHE_LOCK:
        if user_id in SESSION_VALIDATION_CACHE:
            del SESSION_VALIDATION_CACHE[user_id]
            
    await query.answer("Кэш очищен. Обновляю...")
    current_page = 1
    if query.message and query.message.text:
        match = re.search(r'Страница (\d+)', query.message.text)
        if match:
            current_page = int(match.group(1))
    await show_sessions_menu(query.message, query.from_user.id, client_type=client_type, page=current_page)

@router.callback_query(F.data.startswith("delete_session_"))
async def delete_session_callback(query: CallbackQuery):
    if not await check_subscription(query):
        return
    phone_to_delete = query.data.split('_')[-1]
    user_id = query.from_user.id

    client_type = _get_client_type_from_message(query.message)
    if not client_type:
        await query.answer("Ошибка: не удалось определить тип сессий для обновления списка.", show_alert=True)
        logger.warning(f"Could not determine client_type after deleting session for user {user_id}")
        return

    current_page = 1
    if query.message and query.message.text:
        match = re.search(r'Страница (\d+)', query.message.text)
        if match:
            current_page = int(match.group(1))

    await query.answer(f"Удаляю сессию {phone_to_delete}...")
    await db_manager.delete_session(user_id, phone_to_delete)

    async with SESSION_VALIDATION_CACHE_LOCK:
        SESSION_VALIDATION_CACHE.pop(user_id, None)
    await show_sessions_menu(query.message, query.from_user.id, client_type=client_type, page=current_page)


# --- Добавление сессии вручную ---

@router.callback_query(F.data == "add_account")
async def add_account_start(query: CallbackQuery, state: FSMContext):
    if not await check_subscription(query):
        return
    # Prevent starting a new flow if one is already active for this user
    if query.from_user.id in FSM_CLIENTS or query.from_user.id in FSM_TELETHON_CLIENTS:
        await query.answer("❗️ Процесс добавления сессии уже запущен. Завершите или отмените его.", show_alert=True)
        return

    await query.answer()
    await query.message.edit_text("Выберите тип добавляемого аккаунта:", reply_markup=select_client_type_keyboard())
    await state.set_state(SessionStates.choose_client_type)

@router.callback_query(F.data == "client_type_help", SessionStates.choose_client_type)
async def client_type_help_callback(query: CallbackQuery):
    await query.answer()
    await query.message.edit_text(CLIENT_TYPE_HELP_TEXT, reply_markup=select_client_type_keyboard())

@router.callback_query(F.data == "add_session_type_pyrogram", SessionStates.choose_client_type)
async def add_session_pyrogram_selected(query: CallbackQuery, state: FSMContext):
    await state.update_data(client_type='pyrogram')
    await query.message.edit_text("📱 Введите номер телефона для Pyrogram сессии (например, +1234567890).\n/cancel для отмены.")
    await state.set_state(SessionStates.adding_phone)

# --- PYROGRAM AUTHORIZATION FLOW ---

@router.message(SessionStates.adding_phone, F.text, ~F.text.startswith('/'))
async def add_session_phone_received(message: Message, state: FSMContext):
    # --- ИЗМЕНЕНО: Удаляем пробелы из номера для удобства пользователя ---
    phone_number = message.text.replace(" ", "").strip()
    if not re.match(r"^\+\d{10,}$", phone_number):
        await message.reply("❌ Неверный формат номера. Пример: +1234567890.\n/cancel для отмены.")
        return

    user_id = message.from_user.id
    session_name = phone_number.replace('+', '')
    session_dir = os.path.join('sessions', str(user_id))
    session_file_full_path = os.path.join(session_dir, f"{session_name}.session")
    os.makedirs(session_dir, exist_ok=True)

    # Перед началом новой авторизации принудительно удаляем старый файл сессии,
    # чтобы избежать конфликтов версий Pyrogram или поврежденных файлов.
    if os.path.isdir(session_dir):
        # Удаляем все файлы, связанные с этой сессией (.session, .session-journal, .session-wal и т.д.)
        session_name_base = os.path.splitext(os.path.basename(session_file_full_path))[0]
        for filename in os.listdir(session_dir):
            if filename.startswith(session_name_base):
                file_to_delete = os.path.join(session_dir, filename)
                try:
                    if os.path.isfile(file_to_delete):
                        os.remove(file_to_delete)
                        logger.info(f"Removed existing session-related file before re-adding: {file_to_delete}")
                except OSError as e:
                    logger.error(f"Could not remove existing session file {file_to_delete}: {e}")
                    await message.answer("❌ Не удалось удалить старый файл сессии. Пожалуйста, попробуйте снова.")
                    await state.clear()
                    return

    try:
        api_id_as_int = int(config.API_ID)
    except (ValueError, TypeError):
        logger.critical("КРИТИЧЕСКАЯ ОШИБКА: config.API_ID не является валидным числом!")
        await message.answer("❌ Внутренняя ошибка конфигурации. Авторизация невозможна.")
        await state.clear()
        return

    client = FSMClient(
        name=session_name, 
        api_id=api_id_as_int, 
        api_hash=config.API_HASH, 
        workdir=session_dir
    )
    FSM_CLIENTS[user_id] = client  # Store the client instance
    
    try:
        # Connect the client and keep it connected for the FSM flow
        await client.connect()
        sent_code = await client.send_code(phone_number)

        logger.info(f"[{user_id}] Code sent for {phone_number}. Hash: {sent_code.phone_code_hash}")

        # Сохраняем данные для следующего шага в FSM
        await state.update_data(
            phone=phone_number,
            session_name=session_name,
            session_dir=session_dir, # Keep for client init
            phone_code_hash=sent_code.phone_code_hash,
            session_file_full_path=session_file_full_path # Store full path for DB
        )
        
        await message.answer("🔢 Введите код из Telegram:\n/cancel для отмены.")
        await state.set_state(SessionStates.adding_code)

    except FloodWait as e:
        await message.answer(f"❌ Слишком много попыток. Попробуйте через {e.value} сек.")
    except (PhoneNumberInvalid, UserDeactivated):
        await message.answer("❌ Неверный или заблокированный номер телефона.")
    except Exception as e:
        logger.error(f"Unexpected exception during send_code for {phone_number}: {e}", exc_info=True)
        await message.answer(f"❌ Ошибка: {e}")
    finally:
        # --- ИЗМЕНЕНО: Усиленная логика очистки в случае любой ошибки на этом шаге ---
        # Если мы все еще на этом шаге, значит, что-то пошло не так.
        if await state.get_state() == SessionStates.adding_phone:
            client = FSM_CLIENTS.pop(user_id, None)
            if client and client.is_connected:
                await client.disconnect()
            await state.clear()

@router.message(SessionStates.adding_code, F.text, ~F.text.startswith('/'))
async def add_session_code_received(message: Message, state: FSMContext):
    code = message.text.strip()
    data = await state.get_data()
    user_id = message.from_user.id
    
    client = FSM_CLIENTS.get(user_id)
    if not client:
        await message.answer("❌ Произошла внутренняя ошибка или сессия истекла. Пожалуйста, начните заново.", reply_markup=settings_keyboard())
        await state.clear()
        return

    try:
        await client.sign_in(
            phone_number=data['phone'],
            phone_code_hash=data['phone_code_hash'],
            phone_code=code
        )

        # Если мы здесь, вход успешен и 2FA не требуется
        await client.disconnect()
        FSM_CLIENTS.pop(user_id, None)
        await db_manager.add_session(user_id, data['phone'], data['session_file_full_path'], client_type='pyrogram')

        # --- ДОБАВЛЕНО: Инвалидация кэша после успешного добавления сессии ---
        async with SESSION_VALIDATION_CACHE_LOCK:
            SESSION_VALIDATION_CACHE.pop(user_id, None)

        await message.answer(f"✅ Сессия {data['phone']} добавлена!", reply_markup=settings_keyboard())
        await state.clear()
    except SessionPasswordNeeded:
        # 2FA требуется. Файл сессии уже содержит это состояние.
        # The client remains connected for the next step.
        await message.answer("🔐 Введите пароль двухфакторной аутентификации (2FA):\n/cancel для отмены.")
        await state.set_state(SessionStates.adding_password)

    except (PhoneCodeInvalid, PhoneCodeExpired):
        await message.answer("❌ Неверный или истекший код. Попробуйте добавить сессию заново.", reply_markup=settings_keyboard())
        await client.disconnect()
        FSM_CLIENTS.pop(user_id, None)
        await state.clear()
    except (AuthKeyUnregistered, TypeError):
        await message.answer("❌ Ошибка авторизации. Возможно, сессия повреждена. Удалите ее и попробуйте снова.", reply_markup=settings_keyboard())
        await client.disconnect()
        FSM_CLIENTS.pop(user_id, None)
        await state.clear()
    except Exception as e:
        logger.error(f"Error signing in for {data['phone']}: {e}", exc_info=True)
        await message.answer(f"❌ Ошибка: {e}", reply_markup=settings_keyboard())
        await client.disconnect()
        FSM_CLIENTS.pop(user_id, None)
        await state.clear()


@router.message(SessionStates.adding_password, F.text, ~F.text.startswith('/'))
async def add_session_password_received(message: Message, state: FSMContext):
    password = message.text.strip()
    data = await state.get_data()
    user_id = message.from_user.id
    
    client = FSM_CLIENTS.get(user_id)
    if not client:
        await message.answer("❌ Произошла внутренняя ошибка или сессия истекла. Пожалуйста, начните заново.", reply_markup=settings_keyboard())
        await state.clear()
        return

    try:
        await client.check_password(password)

        # Пароль верный, завершаем процесс
        await db_manager.add_session(user_id, data['phone'], data['session_file_full_path'], client_type='pyrogram')

        # --- ДОБАВЛЕНО: Инвалидация кэша после успешного добавления сессии ---
        async with SESSION_VALIDATION_CACHE_LOCK:
            SESSION_VALIDATION_CACHE.pop(user_id, None)

        await message.answer(f"✅ Сессия {data['phone']} добавлена (с 2FA)!", reply_markup=settings_keyboard())

    except Exception as e: # This will now catch errors from check_password
        logger.error(f"Error with 2FA for {data['phone']}: {e}", exc_info=True)
        await message.answer(f"❌ Ошибка с паролем: {e}", reply_markup=settings_keyboard())
    finally:
        # Always clean up at the end of the flow
        if client.is_connected:
            await client.disconnect()
        FSM_CLIENTS.pop(user_id, None)
        await state.clear()


# --- Загрузка сессии файлом (только Pyrogram) ---

@router.callback_query(F.data == "upload_session_file")
async def upload_session_start(query: CallbackQuery, state: FSMContext):
    # --- ИЗМЕНЕНО: Используем новую общую функцию проверки ---
    if not await check_subscription(query):
        return
    # Prevent starting a new flow if one is already active for this user
    if query.from_user.id in FSM_CLIENTS:
        await query.answer("❗️ Процесс добавления сессии уже запущен. Завершите или отмените его.", show_alert=True)
        return

    await query.answer()
    await query.message.edit_text(
        "📤 Отправьте один или несколько файлов сессии <b>Pyrogram</b> (.session).\n"
        "Когда закончите, отправьте /done, или /cancel для отмены."
    )
    await state.set_state(SessionStates.uploading_session)


@router.message(SessionStates.uploading_session, F.document)
async def handle_session_file_upload(message: Message, bot: Bot):
    document = message.document
    if not document.file_name.endswith('.session'):
        await message.reply("❌ Файл должен быть формата .session.")
        return

    user_id = message.from_user.id
    session_dir = os.path.abspath(f"sessions/{user_id}")
    os.makedirs(session_dir, exist_ok=True)
    
    clean_session_name = document.file_name.replace('+', '')
    session_file_path = os.path.join(session_dir, clean_session_name)
    pyrogram_session_name = os.path.splitext(clean_session_name)[0]

    temp_client = None # Initialize temp_client outside try block
    success = False # Flag to track if session was successfully processed
    try:
        # 1. Скачиваем файл
        await bot.download(file=document.file_id, destination=session_file_path)
        
        # 2. Проводим проверку
        temp_client = Client(
            name=pyrogram_session_name, api_id=int(config.API_ID), api_hash=config.API_HASH,
            workdir=session_dir, no_updates=True
        )
        await temp_client.connect()
        me = await temp_client.get_me()
        phone_number = me.phone_number if me else None
        if not phone_number:
            raise ValueError("Не удалось получить номер телефона из файла сессии. Файл может быть поврежден или невалиден.")
        
        # 3. Если все хорошо, добавляем в БД
        await db_manager.add_session(user_id, phone_number, session_file_path, client_type='pyrogram')

        # --- ДОБАВЛЕНО: Инвалидация кэша после успешного добавления сессии ---
        async with SESSION_VALIDATION_CACHE_LOCK:
            SESSION_VALIDATION_CACHE.pop(user_id, None)

        await message.reply(
            f"✅ Файл сессии <code>{html.escape(document.file_name)}</code> успешно проверен и добавлен для номера <code>{html.escape(phone_number)}</code>."
        )
        success = True
    except Exception as e:
        logger.error(f"Ошибка при обработке загруженного файла сессии {document.file_name}: {e}", exc_info=True)
        
        error_text = f"❌ Ошибка при обработке файла сессии: {html.escape(str(e))}"
        e_str = str(e).lower()
        if "no such column" in e_str or "malformed" in e_str:
            error_text += "\n<i>Файл сессии поврежден или несовместим с текущей версией Pyrogram.</i>"
        elif "database is locked" in e_str:
            error_text += "\n<i>База данных сессии временно заблокирована. Попробуйте снова.</i>"
        elif isinstance(e, (AuthKeyUnregistered, UserDeactivated)):
            error_text += "\n<i>Сессия недействительна или устарела.</i>"
        
        await message.reply(f"{error_text}. Файл не был добавлен.")
    finally:
        if temp_client and temp_client.is_connected:
            await temp_client.disconnect()
        
        if not success and os.path.exists(session_file_path):
            try:
                os.remove(session_file_path)
            except OSError as e:
                logger.error(f"Не удалось удалить проблемный файл сессии {session_file_path}: {e}")


@router.message(SessionStates.uploading_session, F.text == '/done')
async def finish_session_upload(message: Message, state: FSMContext):
    await message.answer("✅ Загрузка файлов сессий завершена.", reply_markup=settings_keyboard())
    await state.clear()


# --- TELETHON AUTHORIZATION FLOW ---

@router.callback_query(F.data == "add_session_type_telethon", SessionStates.choose_client_type)
async def add_session_telethon_selected(query: CallbackQuery, state: FSMContext):
    await state.update_data(client_type='telethon')
    await query.message.edit_text("📱 Введите номер телефона для Telethon сессии (например, +1234567890).\n/cancel для отмены.")
    await state.set_state(SessionStates.adding_phone_telethon)

@router.message(SessionStates.adding_phone_telethon, F.text, ~F.text.startswith('/'))
async def add_session_phone_telethon_received(message: Message, state: FSMContext):
    # --- ИЗМЕНЕНО: Удаляем пробелы из номера для удобства пользователя ---
    phone_number = message.text.replace(" ", "").strip()
    if not re.match(r"^\+\d{10,}$", phone_number):
        await message.reply("❌ Неверный формат номера. Пример: +1234567890.\n/cancel для отмены.")
        return

    user_id = message.from_user.id
    session_name = phone_number.replace('+', '')
    session_dir = os.path.join('sessions', str(user_id))
    session_file_full_path = os.path.join(session_dir, f"{session_name}.session")
    os.makedirs(session_dir, exist_ok=True)

    # Удаляем старый файл сессии, если он существует
    if os.path.exists(session_file_full_path):
        os.remove(session_file_full_path)

    client = TelegramClient(StringSession(), int(config.API_ID), config.API_HASH)
    FSM_TELETHON_CLIENTS[user_id] = client

    try:
        await client.connect()
        sent_code = await client.send_code_request(phone_number)
        await state.update_data(
            phone=phone_number,
            phone_code_hash=sent_code.phone_code_hash,
            session_file_full_path=session_file_full_path
        )
        await message.answer("🔢 Введите код из Telegram:\n/cancel для отмены.")
        await state.set_state(SessionStates.adding_code_telethon)
    except FloodWaitError as e:
        await message.answer(f"❌ Слишком много попыток. Попробуйте через {e.seconds} сек.")
        if client.is_connected(): await client.disconnect()
        FSM_TELETHON_CLIENTS.pop(user_id, None)
        await state.clear()
    except (PhoneNumberBannedError, UserDeactivatedError):
        await message.answer("❌ Неверный или заблокированный номер телефона.")
        if client.is_connected(): await client.disconnect()
        FSM_TELETHON_CLIENTS.pop(user_id, None)
        await state.clear()
    except Exception as e:
        logger.error(f"Unexpected exception during Telethon send_code for {phone_number}: {e}", exc_info=True)
        await message.answer(f"❌ Ошибка: {e}")
        if client.is_connected(): await client.disconnect()
        FSM_TELETHON_CLIENTS.pop(user_id, None)
        await state.clear()

@router.message(SessionStates.adding_code_telethon, F.text, ~F.text.startswith('/'))
async def add_session_code_telethon_received(message: Message, state: FSMContext):
    code = message.text.strip()
    data = await state.get_data()
    user_id = message.from_user.id
    client = FSM_TELETHON_CLIENTS.get(user_id)

    if not client:
        await message.answer("❌ Произошла внутренняя ошибка или сессия истекла. Пожалуйста, начните заново.", reply_markup=settings_keyboard())
        await state.clear()
        return

    try:
        await client.sign_in(phone=data['phone'], code=code, phone_code_hash=data['phone_code_hash'])
        # Если мы здесь, вход успешен и 2FA не требуется
        session_string = client.session.save()
        with open(data['session_file_full_path'], "w") as f:
            f.write(session_string)
        await db_manager.add_session(user_id, data['phone'], data['session_file_full_path'], client_type='telethon')
        await message.answer(f"✅ Сессия Telethon {data['phone']} добавлена!", reply_markup=settings_keyboard())
    except SessionPasswordNeededError:
        await message.answer("🔐 Введите пароль двухфакторной аутентификации (2FA):\n/cancel для отмены.")
        await state.set_state(SessionStates.adding_password_telethon)
        return # Не выходим из finally, так как клиент нужен для следующего шага
    except (PhoneCodeInvalidError, AuthKeyUnregisteredError):
        await message.answer("❌ Неверный или истекший код. Попробуйте добавить сессию заново.", reply_markup=settings_keyboard())
    except Exception as e:
        logger.error(f"Error signing in with Telethon for {data['phone']}: {e}", exc_info=True)
        await message.answer(f"❌ Ошибка: {e}", reply_markup=settings_keyboard())
    finally:
        current_state = await state.get_state()
        if current_state != SessionStates.adding_password_telethon:
            if client.is_connected(): await client.disconnect()
            FSM_TELETHON_CLIENTS.pop(user_id, None)
            await state.clear()

@router.message(SessionStates.adding_password_telethon, F.text, ~F.text.startswith('/'))
async def add_session_password_telethon_received(message: Message, state: FSMContext):
    password = message.text.strip()
    data = await state.get_data()
    user_id = message.from_user.id
    client = FSM_TELETHON_CLIENTS.get(user_id)

    try:
        await client.sign_in(password=password)
        session_string = client.session.save()
        with open(data['session_file_full_path'], "w") as f:
            f.write(session_string)
        await db_manager.add_session(user_id, data['phone'], data['session_file_full_path'], client_type='telethon')
        await message.answer(f"✅ Сессия Telethon {data['phone']} добавлена (с 2FA)!", reply_markup=settings_keyboard())
    except Exception as e:
        await message.answer(f"❌ Ошибка с паролем: {e}", reply_markup=settings_keyboard())
    finally:
        if client and client.is_connected(): await client.disconnect()
        FSM_TELETHON_CLIENTS.pop(user_id, None)
        await state.clear()