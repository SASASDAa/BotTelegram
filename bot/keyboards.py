# keyboards.py
import html

from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton

import config

# --- Reply Keyboards ---

def main_keyboard() -> ReplyKeyboardMarkup:
    """Создает главную клавиатуру."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="🚀 Задачи"),  # Spam, Attack
                KeyboardButton(text="🔥 Прогрев"),  # Warmer
            ],
            [
                KeyboardButton(text="👤 Сбор аудитории"),
                KeyboardButton(text="⚙️ Настройки"),
            ],
            [
                KeyboardButton(text="📊 Статус"),
                KeyboardButton(text="👤 Профиль"),
                KeyboardButton(text="🛒 Магазин"),
            ],
        ],
        resize_keyboard=True
    )

def tasks_keyboard(is_spam_active: bool, is_attack_active: bool) -> ReplyKeyboardMarkup:
    """Создает клавиатуру для управления задачами."""
    spam_btn_text = "🛑 Остановить спам" if is_spam_active else "▶️ Спам в группы"
    attack_btn_text = "🛑 Остановить атаку" if is_attack_active else "💥 Атака в ЛС"
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=spam_btn_text)],
            [KeyboardButton(text=attack_btn_text)],
            [KeyboardButton(text="⚙️ Настройки задач")],
            [KeyboardButton(text="🔙 В меню")]
        ],
        resize_keyboard=True
    )

def settings_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📱 Сессии"), KeyboardButton(text="📢 Группы"), KeyboardButton(text="✏️ Тексты")],
            [KeyboardButton(text="🌐 Прокси"), KeyboardButton(text="🗓️ Планировщик"), KeyboardButton(text="🤖 Настройки ИИ")],
            [KeyboardButton(text="🔄 Сброс данных"), KeyboardButton(text="🔙 В меню")],
        ],
        resize_keyboard=True
    )

def reset_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🗑️ Сессии"), KeyboardButton(text="🗑️ Группы")],
            [KeyboardButton(text="🗑️ Тексты"), KeyboardButton(text="🗑️ Прокси")],
            [KeyboardButton(text="🗑️ Всё")],
            [KeyboardButton(text="🔙 В меню")]
        ],
        resize_keyboard=True
    )

def warmer_menu_keyboard(is_active: bool) -> ReplyKeyboardMarkup:
    """Создает клавиатуру для меню прогрева."""
    start_stop_btn = "🛑 Остановить прогрев" if is_active else "🔥 Начать прогрев"
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=start_stop_btn)],
            [KeyboardButton(text="⚙️ Настройки прогрева")],
            [KeyboardButton(text="📖 Что такое прогрев?")],
            [KeyboardButton(text="🔙 В меню")]
        ],
        resize_keyboard=True
    )

def warmer_settings_main_keyboard(settings: dict) -> InlineKeyboardMarkup:
    """Создает ГЛАВНУЮ клавиатуру для настроек прогрева с разделами."""
    duration = settings.get('duration_days', 7)
    buttons = [
        [InlineKeyboardButton(text="📈 Лимиты и действия", callback_data="warmer_show_limits")],
        [InlineKeyboardButton(text="🎯 Контент (каналы, фразы)", callback_data="warmer_show_content")],
        [InlineKeyboardButton(text="⚙️ Поведение (расписание, диалоги)", callback_data="warmer_show_behavior")],
        [InlineKeyboardButton(text=f"⏳ Длительность: {duration} дн.", callback_data="warmer_set_duration")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def warmer_settings_limits_keyboard(settings: dict) -> InlineKeyboardMarkup:
    """Создает клавиатуру для подменю 'Лимиты и действия'."""
    buttons = [
        [InlineKeyboardButton(text=f"📥 Вступлений в день: {settings.get('join_channels_per_day', 2)}", callback_data="warmer_set_joins")],
        [InlineKeyboardButton(text=f"👍 Реакций в день: {settings.get('send_reactions_per_day', 5)}", callback_data="warmer_set_reactions")],
        [InlineKeyboardButton(text=f"💬 Диалогов в день: {settings.get('dialogues_per_day', 3)}", callback_data="warmer_set_dialogues")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="warmer_back_to_main")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def warmer_settings_content_keyboard(settings: dict) -> InlineKeyboardMarkup:
    """Создает клавиатуру для подменю 'Контент'."""
    target_channels_val = settings.get('target_channels', '')
    channels_status = f"{target_channels_val[:20]}..." if target_channels_val else "не заданы"
    phrases_val = settings.get('dialogue_phrases', '')
    phrases_status = f"{phrases_val[:20]}..." if phrases_val else "не заданы"
    buttons = [
        [InlineKeyboardButton(text=f"🎯 Целевые каналы: {channels_status}", callback_data="warmer_set_channels")],
        [InlineKeyboardButton(text=f"📝 Фразы для диалогов: {phrases_status}", callback_data="warmer_set_phrases")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="warmer_back_to_main")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def warmer_settings_behavior_keyboard(settings: dict) -> InlineKeyboardMarkup:
    """Создает клавиатуру для подменю 'Поведение'."""
    dialogue_status = "Вкл ✅" if settings.get('dialogue_simulation_enabled') else "Выкл ❌"
    schedule_enabled = settings.get('active_hours_enabled')
    schedule_start = settings.get('active_hours_start', 9)
    schedule_end = settings.get('active_hours_end', 22)
    schedule_status_text = f" ({schedule_start:02}:00 - {schedule_end:02}:00 по МСК)" if schedule_enabled else ""
    schedule_status = f"Вкл{schedule_status_text} ✅" if schedule_enabled else "Выкл ❌"
    inform_status = "Вкл ✅" if settings.get('inform_user_on_action') else "Выкл ❌"
    buttons = [
        [InlineKeyboardButton(text=f"💬 Диалоги: {dialogue_status}", callback_data="warmer_toggle_dialogue")],
        [InlineKeyboardButton(text=f"⏰ Расписание: {schedule_status}", callback_data="warmer_toggle_schedule")],
        [InlineKeyboardButton(text="🔧 Настроить расписание", callback_data="warmer_set_schedule")],
        [InlineKeyboardButton(text=f"🔔 Информирование: {inform_status}", callback_data="warmer_toggle_inform")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="warmer_back_to_main")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def spam_settings_keyboard(settings: dict) -> InlineKeyboardMarkup:
    """Создает клавиатуру для настроек спама."""
    delay = settings.get("delay", config.MIN_DELAY_BETWEEN_COMMENTS)
    persistent_status = "Вкл ✅" if settings.get("persistent_spam", False) else "Выкл ❌"
    auto_leave_status = "Вкл ✅" if settings.get("auto_leave_enabled", False) else "Выкл ❌"
    
    buttons = [
        [InlineKeyboardButton(text=f"⏱ Задержка между сообщениями: {delay} сек.", callback_data="spam_set_delay")],
        [
            InlineKeyboardButton(text=f"🔁 Постоянный спам: {persistent_status}", callback_data="spam_toggle_persistent"),
            InlineKeyboardButton(text=f"📤 Автовыход: {auto_leave_status}", callback_data="spam_toggle_auto_leave")
        ],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_tasks_menu")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# --- Inline Keyboards ---

def sessions_keyboard_markup(session_statuses_page: list[dict], current_page: int, total_pages: int, client_type: str) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="🔄 Обновить список", callback_data="refresh_sessions")]
    ]

    for session in session_statuses_page:
        phone = session.get('phone', 'N/A')
        status = session.get('status', 'N/A')
        session_client_type = session.get('client_type', 'pyrogram')
        type_icon = "T" if session_client_type == 'telethon' else "P"
        button_text = f"[{type_icon}] {status} | {phone}"
        buttons.append([InlineKeyboardButton(text=button_text, callback_data=f"delete_session_{phone}")])

    if total_pages > 1:
        nav_row = []
        if current_page > 1:
            nav_row.append(InlineKeyboardButton(text="⬅️", callback_data=f"sessions_page_{current_page - 1}"))

        nav_row.append(InlineKeyboardButton(text=f"· {current_page}/{total_pages} ·", callback_data="noop_answer"))

        if current_page < total_pages:
            nav_row.append(InlineKeyboardButton(text="➡️", callback_data=f"sessions_page_{current_page + 1}"))

        if nav_row:
            buttons.append(nav_row)

    buttons.append([InlineKeyboardButton(text="➕ Добавить аккаунт", callback_data="add_account")])
    # --- ИЗМЕНЕНО: Показываем кнопку загрузки только для Pyrogram ---
    if client_type == 'pyrogram':
        buttons.append([InlineKeyboardButton(text="📤 Загрузить файл .session (Pyrogram)", callback_data="upload_session_file")])
    
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def select_client_type_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура для выбора типа клиента при добавлении сессии."""
    buttons = [
        [InlineKeyboardButton(text="Pyrogram (Стабильный)", callback_data="add_session_type_pyrogram")],
        [InlineKeyboardButton(text="Telethon (Для атак)", callback_data="add_session_type_telethon")],
        [InlineKeyboardButton(text="В чем разница?", callback_data="client_type_help")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def chats_keyboard_markup(chats_list_page: list, current_page: int, total_pages: int) -> InlineKeyboardMarkup:
    buttons = []
    for chat_id_str in chats_list_page:
        buttons.append([InlineKeyboardButton(text=f"❌ Удалить {html.escape(chat_id_str[:40])}...", callback_data=f"delete_chat_{chat_id_str}")])

    if total_pages > 1:
        nav_row = []
        if current_page > 1:
            nav_row.append(InlineKeyboardButton(text="⬅️", callback_data=f"chats_page_{current_page - 1}"))

        nav_row.append(InlineKeyboardButton(text=f"· {current_page}/{total_pages} ·", callback_data="noop_answer"))

        if current_page < total_pages:
            nav_row.append(InlineKeyboardButton(text="➡️", callback_data=f"chats_page_{current_page + 1}"))

        if nav_row:
            buttons.append(nav_row)

    buttons.append([
        InlineKeyboardButton(text="➕ Добавить списком", callback_data="add_chats_list"),
        InlineKeyboardButton(text="🔍 Найти группы", callback_data="find_chats")
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def comments_menu_keyboard(has_photo: bool) -> InlineKeyboardMarkup:
    """Создает клавиатуру для меню текстов и медиа."""
    buttons = [
        [InlineKeyboardButton(text="📝 Изменить тексты (вручную/файлом)", callback_data="edit_spam_texts")],
        [InlineKeyboardButton(text="🖼️ Добавить/Изменить фото", callback_data="add_spam_photo")]
    ]
    if has_photo:
        buttons.append([InlineKeyboardButton(text="🗑️ Удалить фото", callback_data="delete_spam_photo")])
    
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def proxies_keyboard_markup(
    proxies_list_page: list, current_page: int, total_pages: int, use_proxy: bool
) -> InlineKeyboardMarkup:
    buttons = []
    for proxy_str in proxies_list_page:
        buttons.append([InlineKeyboardButton(text=f"❌ Удалить {html.escape(proxy_str[:40])}", callback_data=f"delete_proxy_{proxy_str}")])

    if total_pages > 1:
        nav_row = []
        if current_page > 1:
            nav_row.append(InlineKeyboardButton(text="⬅️", callback_data=f"proxies_page_{current_page - 1}"))

        nav_row.append(InlineKeyboardButton(text=f"· {current_page}/{total_pages} ·", callback_data="noop_answer"))

        if current_page < total_pages:
            nav_row.append(InlineKeyboardButton(text="➡️", callback_data=f"proxies_page_{current_page + 1}"))

        if nav_row:
            buttons.append(nav_row)

    status_text = "Включено ✅" if use_proxy else "Выключено ❌"
    buttons.append([InlineKeyboardButton(text=f"Использовать прокси: {status_text}", callback_data="toggle_proxy_usage")])

    buttons.append([InlineKeyboardButton(text="➕ Добавить прокси", callback_data="add_proxy")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def ai_settings_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Генерирует клавиатуру для настроек ИИ."""
    from .database.db_manager import db_manager # Local import to prevent circular dependencies
    ai_settings = await db_manager.get_ai_settings(user_id)
    status_uniqueness = "Включена ✅" if ai_settings["enabled"] else "Выключена ❌"
    api_key_btn_text = "🔑 API Ключ (Установлен)" if ai_settings["api_key"] else "🔑 API Ключ (Не установлен)"
    buttons = [
        [InlineKeyboardButton(text=api_key_btn_text, callback_data="set_gemini_key")],
        [InlineKeyboardButton(text="📝 Промпт уникализации", callback_data="set_gemini_prompt")],
        [InlineKeyboardButton(text=f"✨ Уникализация: {status_uniqueness}", callback_data="toggle_uniqueness")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def admin_keyboard(is_super_admin: bool = False) -> ReplyKeyboardMarkup:
    kb = [
        [KeyboardButton(text="📊 Статистика бота"), KeyboardButton(text="📢 Рассылка")],
        [KeyboardButton(text="➕ Выдать подписку"), KeyboardButton(text="➖ Отозвать подписку")],
        [KeyboardButton(text="🔍 Информация о юзере"), KeyboardButton(text="🚫 Бан/Разбан юзера")],
        [KeyboardButton(text="🎁 Промокоды")],
    ]
    if is_super_admin:
        kb.append([
            KeyboardButton(text="👑 Управление админами"),
            KeyboardButton(text="⚙️ Настройки магазина")
        ])
        kb.append([
            KeyboardButton(text="🛠️ Тех. работы"),
            KeyboardButton(text="🔄 Перезагрузка бота")
        ])
    
    kb.append([KeyboardButton(text="🔙 В меню")])
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

def admin_ban_confirm_keyboard(user_id: int, is_banned: bool) -> InlineKeyboardMarkup:
    if is_banned:
        action_btn = InlineKeyboardButton(text="✅ Разбанить", callback_data=f"unban_user_{user_id}")
    else:
        action_btn = InlineKeyboardButton(text="🚫 Забанить", callback_data=f"ban_user_{user_id}")
    
    buttons = [
        [action_btn]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def admin_broadcast_confirm_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="✅ Подтвердить", callback_data="confirm_broadcast")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def admin_restart_confirm_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="✅ Да, перезагрузить", callback_data="confirm_restart")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

async def admin_shop_settings_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура настроек магазина."""
    from .database.db_manager import db_manager # Local import to prevent circular dependencies
    support_contact = await db_manager.get_bot_setting('support_contact') or config.SUPPORT_CONTACT
    show_buy_sessions_val = await db_manager.get_bot_setting('show_buy_sessions_button')
    
    show_sessions_text = "Вкл ✅" if show_buy_sessions_val != '0' else "Выкл ❌"

    buttons = [
        [InlineKeyboardButton(text=f"Контакт: {support_contact}", callback_data="admin_set_support_contact")],
        [InlineKeyboardButton(text=f"Кнопка 'Купить сессии': {show_sessions_text}", callback_data="admin_toggle_buy_sessions")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def admin_promo_menu_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="➕ Создать промокод", callback_data="admin_create_promo")],
        [InlineKeyboardButton(text="📋 Список промокодов", callback_data="admin_list_promo")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def admin_promo_type_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="Одноразовый (1 активация)", callback_data="promo_type_single")],
        [InlineKeyboardButton(text="Многоразовый", callback_data="promo_type_reusable")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

async def profile_inline_keyboard() -> InlineKeyboardMarkup:
    """Создает клавиатуру для профиля."""
    from .database.db_manager import db_manager # Local import to prevent circular dependencies
    support_contact = await db_manager.get_bot_setting('support_contact') or config.SUPPORT_CONTACT
    buttons = [
        [InlineKeyboardButton(text="🎁 Активировать промокод", callback_data="activate_promo_code")],
        [InlineKeyboardButton(text="💬 Техподдержка", url=support_contact)]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def maintenance_keyboard() -> ReplyKeyboardMarkup:
    """A simple keyboard for maintenance mode."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="/start")]
        ],
        resize_keyboard=True
    )


def admin_promo_list_keyboard(promo_codes: list) -> InlineKeyboardMarkup:
    buttons = []
    for promo in promo_codes:
        max_act = promo['max_activations']
        cur_act = promo['current_activations']
        
        if max_act == 1:
            status_icon = "✅" if cur_act > 0 else "⏳"
            act_text = "Активирован" if cur_act > 0 else "Не активирован"
        else:
            status_icon = "✅" if max_act != 0 and cur_act >= max_act else "⏳"
            limit_text = "∞" if max_act == 0 else max_act
            act_text = f"{cur_act}/{limit_text} активаций"

        text = f"{status_icon} {promo['code']} ({promo['duration_days']}д) - {act_text}"
        buttons.append([
            InlineKeyboardButton(text=text, callback_data=f"view_promo_{promo['code']}"),
            InlineKeyboardButton(text="❌", callback_data=f"admin_delete_promo_{promo['code']}")
        ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def manage_admins_keyboard(admins: list[dict]) -> InlineKeyboardMarkup:
    buttons = []
    for admin in admins:
        role_emoji = "👑" if admin['role'] == 'super_admin' else "🧑‍💼"
        username = f"(@{admin['username']})" if admin['username'] else ""
        text = f"{role_emoji} {admin['user_id']} {username}"
        
        # Super admin cannot be removed
        if admin['role'] != 'super_admin':
            buttons.append([
                InlineKeyboardButton(text=text, callback_data="noop_answer"),
                InlineKeyboardButton(text="❌", callback_data=f"remove_admin_{admin['user_id']}")
            ])
        else:
            buttons.append([InlineKeyboardButton(text=text, callback_data="noop_answer")])
            
    buttons.append([InlineKeyboardButton(text="➕ Добавить администратора", callback_data="add_admin")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

async def shop_keyboard() -> InlineKeyboardMarkup:
    """Создает клавиатуру для магазина."""
    from .database.db_manager import db_manager # Local import to prevent circular dependencies
    support_contact = await db_manager.get_bot_setting('support_contact') or config.SUPPORT_CONTACT
    show_buy_sessions = await db_manager.get_bot_setting('show_buy_sessions_button')
    
    buttons = [
        [InlineKeyboardButton(text="💳 Купить подписку", url=support_contact)],
    ]
    # По умолчанию кнопка показана, если настройка не равна '0'
    if show_buy_sessions != '0':
        buttons.append([InlineKeyboardButton(text="📱 Купить сессии", url=support_contact)])
        
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def attack_menu_keyboard(data: dict) -> InlineKeyboardMarkup:
    """Генерирует клавиатуру для меню настройки атаки в ЛС."""
    attack_mode = data.get('attack_mode', 'single')
    use_ai = data.get('attack_use_ai', False)
    ai_btn_text = "🤖 ИИ: Вкл ✅" if use_ai else "🤖 ИИ: Выкл ❌"

    is_infinite = data.get('attack_is_infinite', False)
    infinite_btn_text = "🔁 Бесконечная: Вкл ✅" if is_infinite else "🔁 Бесконечная: Выкл ❌"

    skip_admins = data.get('attack_skip_admins', True)
    skip_admins_btn_text = "🚫 Пропускать админов: Вкл ✅" if skip_admins else "🚫 Пропускать админов: Выкл ❌"

    target_btn_text = "🎯 Цель: Юзер/Группа" if attack_mode == 'single' else "🎯 Цель: Собранная база"
    target_set_btn = InlineKeyboardButton(text="👤 Указать цель", callback_data="attack_set_nickname") if attack_mode == 'single' else InlineKeyboardButton(text="✅ База выбрана", callback_data="noop_answer")

    keyboard = [
        [
            InlineKeyboardButton(text=target_btn_text, callback_data="attack_toggle_mode"),
            target_set_btn
        ],
        [
            InlineKeyboardButton(text="💬 Кол-во", callback_data="attack_set_count"),
            InlineKeyboardButton(text="⏱ Задержка", callback_data="attack_set_delay"),
        ],
        [
            InlineKeyboardButton(text=ai_btn_text, callback_data="attack_toggle_ai"),
            InlineKeyboardButton(text=skip_admins_btn_text, callback_data="attack_toggle_skip_admins")
        ],
        [InlineKeyboardButton(text=infinite_btn_text, callback_data="attack_toggle_infinite")],
        [InlineKeyboardButton(text="🚀 Начать атаку", callback_data="attack_start")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def attack_flood_wait_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура для уведомления о FloodWait в атаке."""
    buttons = [
        [InlineKeyboardButton(text="❓ Что это значит и что делать?", callback_data="attack_flood_help")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def scraper_menu_keyboard(scraped_count: int, filter_level: str) -> InlineKeyboardMarkup:
    """Создает клавиатуру для меню парсинга."""
    filter_map = {
        "all": "Всех",
        "recent": "Недавно онлайн",
        "week": "Онлайн за неделю"
    }
    filter_text = filter_map.get(filter_level, "Всех")

    buttons = [
        [InlineKeyboardButton(text=f"👤 Собрано юзеров: {scraped_count}", callback_data="noop_answer")],
        [InlineKeyboardButton(text=f"🎯 Фильтр сбора: {filter_text}", callback_data="scraper_toggle_filter")],
        [InlineKeyboardButton(text="▶️ Начать новый сбор", callback_data="scraper_start_new")],
        [
            InlineKeyboardButton(text="📤 Выгрузить базу", callback_data="scraper_export"),
            InlineKeyboardButton(text="📥 Загрузить базу", callback_data="scraper_import")
        ],
        [InlineKeyboardButton(text="🗑️ Очистить базу", callback_data="scraper_clear_all")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def scheduler_menu_keyboard(tasks: list[dict]) -> InlineKeyboardMarkup:
    """Создает клавиатуру для меню планировщика."""
    buttons = []
    task_type_map = {
        'spam': '▶️ Спам в группы',
        'attack': '💥 Атака в ЛС'
    }
    for task in tasks:
        task_type_text = task_type_map.get(task['task_type'], 'Неизвестная задача')
        
        # Форматируем время следующего запуска, если оно есть
        next_run_time_str = ""
        if task.get('next_run_time'):
            # Форматируем в "ДД.ММ ЧЧ:ММ МСК"
            next_run_time_str = f" | 🕒 {task['next_run_time'].strftime('%d.%m %H:%M')} МСК"

        buttons.append([
            InlineKeyboardButton(text=f"{task_type_text} ({task['cron']}){next_run_time_str}", callback_data="noop_answer"),
            InlineKeyboardButton(text="❌", callback_data=f"delete_task_{task['job_id']}")
        ])
    
    buttons.append([InlineKeyboardButton(text="➕ Создать новую задачу", callback_data="schedule_new_task")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def scheduler_task_type_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура для выбора типа задачи для планирования."""
    buttons = [
        [InlineKeyboardButton(text="▶️ Спам в группы", callback_data="schedule_type_spam")],
        [InlineKeyboardButton(text="💥 Атака в ЛС", callback_data="schedule_type_attack")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def select_sessions_keyboard(total_sessions: int, task_prefix: str) -> InlineKeyboardMarkup:
    """Создает клавиатуру для выбора количества сессий для задачи."""
    buttons = []
    options = [5, 10, 20, 50]
    row = []
    for opt in options:
        if total_sessions >= opt:
            row.append(InlineKeyboardButton(text=str(opt), callback_data=f"{task_prefix}_sessions_{opt}"))
    if row:
        buttons.append(row)

    buttons.append([
        InlineKeyboardButton(text=f"Все ({total_sessions})", callback_data=f"{task_prefix}_sessions_all"),
        InlineKeyboardButton(text="Своё число", callback_data=f"{task_prefix}_sessions_custom")
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)