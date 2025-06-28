# bot/handlers/scheduler_handler.py
import json
import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.types.inline_keyboard_markup import InlineKeyboardMarkup
from aiogram.types.inline_keyboard_button import InlineKeyboardButton
from apscheduler.triggers.cron import CronTrigger

import config
from bot.database.db_manager import db_manager
from bot.keyboards import (
    scheduler_menu_keyboard, scheduler_task_type_keyboard, settings_keyboard
)
from bot.middlewares import check_subscription
from bot.scheduler_manager import scheduler_manager
from bot.states import SchedulerStates

router = Router()
logger = logging.getLogger(__name__)

async def show_scheduler_menu(message: Message, user_id: int):
    """Displays the scheduler menu for a user."""
    tasks_from_db = await db_manager.get_scheduled_tasks_for_user(user_id)
    
    # Обогащаем задачи информацией о времени следующего запуска
    enriched_tasks = []
    if scheduler_manager:
        for task in tasks_from_db:
            job_details = scheduler_manager.get_job_details(task['job_id'])
            if job_details:
                task['next_run_time'] = job_details.get('next_run_time')
            enriched_tasks.append(task)
    else:
        enriched_tasks = tasks_from_db

    text = "🗓️ <b>Планировщик задач</b>\n\nЗдесь вы можете настроить автоматический запуск задач по расписанию."
    if not enriched_tasks:
        text += "\n\nУ вас пока нет запланированных задач."
    
    await message.answer(text, reply_markup=scheduler_menu_keyboard(enriched_tasks))

@router.message(F.text == "🗓️ Планировщик")
async def scheduler_menu_command(message: Message):
    await show_scheduler_menu(message, message.from_user.id)

@router.callback_query(F.data.startswith("delete_task_"))
async def delete_scheduled_task(query: CallbackQuery):
    # --- ДОБАВЛЕНО: Проверка подписки ---
    if not await check_subscription(query):
        return
    job_id = query.data.split('_')[-1]
    if not scheduler_manager:
        await query.answer("❌ Планировщик неактивен.", show_alert=True)
        return
    
    await scheduler_manager.remove_task(job_id)
    await query.answer("✅ Задача удалена.", show_alert=True)
    
    tasks = await db_manager.get_scheduled_tasks_for_user(query.from_user.id)
    await query.message.edit_reply_markup(reply_markup=scheduler_menu_keyboard(tasks))

@router.callback_query(F.data == "schedule_new_task")
async def schedule_new_task_start(query: CallbackQuery, state: FSMContext):
    # --- ДОБАВЛЕНО: Проверка подписки ---
    if not await check_subscription(query):
        return
    await state.set_state(SchedulerStates.choose_task_type)
    await query.message.edit_text("Выберите тип задачи для планирования:", reply_markup=scheduler_task_type_keyboard())
    await query.answer()

@router.callback_query(F.data == "schedule_cancel")
async def schedule_cancel(query: CallbackQuery, state: FSMContext):
    await state.clear()
    await query.message.delete()
    await query.answer()

@router.callback_query(F.data == "schedule_type_spam", SchedulerStates.choose_task_type)
async def schedule_spam_chosen(query: CallbackQuery, state: FSMContext):
    await state.update_data(task_type='spam')
    await state.set_state(SchedulerStates.enter_cron)
    await query.message.edit_text(
        "Введите расписание в формате <b>CRON</b>.\n\n"
        "<code>минута час день_месяца месяц день_недели</code>\n\n"
        "<b>Примеры:</b>\n"
        "• <code>0 9 * * *</code> — каждый день в 9:00\n"
        "• <code>*/30 * * * *</code> — каждые 30 минут\n"
        "• <code>0 12 * * 1-5</code> — по будням в 12:00\n\n"
        "Используйте /cancel для отмены."
    )
    await query.answer()

@router.callback_query(F.data == "schedule_type_attack", SchedulerStates.choose_task_type)
async def schedule_attack_chosen(query: CallbackQuery, state: FSMContext):
    await state.update_data(task_type='attack')
    await state.set_state(SchedulerStates.enter_cron)
    await query.message.edit_text(
        "Введите расписание в формате <b>CRON</b>.\n\n"
        "<code>минута час день_месяца месяц день_недели</code>\n\n"
        "<b>Примеры:</b>\n"
        "• <code>0 9 * * *</code> — каждый день в 9:00\n"
        "• <code>*/30 * * * *</code> — каждые 30 минут\n"
        "• <code>0 12 * * 1-5</code> — по будням в 12:00\n\n"
        "Используйте /cancel для отмены."
    )
    await query.answer()

@router.message(SchedulerStates.enter_cron)
async def schedule_cron_received(message: Message, state: FSMContext):
    cron_expression = message.text.strip()
    try:
        CronTrigger.from_crontab(cron_expression)
    except ValueError as e:
        await message.reply(f"❌ Неверный формат CRON: {e}. Попробуйте снова.")
        return

    await state.update_data(cron=cron_expression)
    data = await state.get_data()
    
    if data['task_type'] == 'spam':
        await state.set_state(SchedulerStates.enter_spam_params)
        await message.answer("Введите количество сессий для задачи спама.\nОтправьте '<b>all</b>' для использования всех доступных сессий.")
    elif data['task_type'] == 'attack':
        await state.set_state(SchedulerStates.enter_attack_mode)
        markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🎯 Один юзер", callback_data="sched_attack_mode_single")],
            [InlineKeyboardButton(text="👥 Собранная база", callback_data="sched_attack_mode_mass")]
        ])
        await message.answer("Выберите режим атаки:", reply_markup=markup)


@router.message(SchedulerStates.enter_spam_params)
async def schedule_spam_params_received(message: Message, state: FSMContext):
    session_input = message.text.strip().lower()
    session_limit = None
    if session_input != 'all':
        try:
            session_limit = int(session_input)
            if session_limit <= 0:
                await message.reply("❌ Количество сессий должно быть положительным числом.")
                return
        except ValueError:
            await message.reply("❌ Введите число или 'all'.")
            return
    
    if not scheduler_manager:
        await message.answer("❌ Планировщик неактивен. Невозможно создать задачу.", reply_markup=settings_keyboard())
        await state.clear()
        return

    data = await state.get_data()
    task_params = {'session_limit': session_limit}
    
    await scheduler_manager.add_task(user_id=message.from_user.id, task_type=data['task_type'], cron_expression=data['cron'], task_params=task_params)
    
    await message.answer("✅ Новая задача успешно запланирована!", reply_markup=settings_keyboard())
    await state.clear()


# --- Attack Scheduling Handlers ---

@router.callback_query(F.data.startswith("sched_attack_mode_"), SchedulerStates.enter_attack_mode)
async def schedule_attack_mode_chosen(query: CallbackQuery, state: FSMContext):
    mode = query.data.split('_')[-1]  # 'single' or 'mass'
    await state.update_data(attack_mode=mode)

    if mode == 'single':
        await state.set_state(SchedulerStates.enter_attack_target)
        await query.message.edit_text("Введите никнейм цели (например, @username).")
    else:  # mass
        await state.set_state(SchedulerStates.enter_attack_count)
        await query.message.edit_text("Введите количество сообщений для отправки на 1 юзера из базы.")
    await query.answer()


@router.message(SchedulerStates.enter_attack_target)
async def schedule_attack_target_received(message: Message, state: FSMContext):
    target_input = message.text.strip()

    # Нормализуем ввод, чтобы Pyrogram правильно обработал полные ссылки
    # и обычные юзернеймы, как и в attack_handler.
    normalized_target = target_input
    if normalized_target.startswith(('http://t.me/', 'https://t.me/')):
        path_part = normalized_target.split('t.me/')[1]
        if not path_part.startswith(('joinchat', '+')):
            normalized_target = '@' + path_part.split('/')[0]
    elif not normalized_target.startswith('@'):
        normalized_target = '@' + normalized_target

    await state.update_data(target_nickname=normalized_target)
    await state.set_state(SchedulerStates.enter_attack_count)
    await message.answer("Введите количество сообщений для отправки.")


@router.message(SchedulerStates.enter_attack_count)
async def schedule_attack_count_received(message: Message, state: FSMContext):
    try:
        count = int(message.text.strip())
        if count < 1:
            await message.reply("❌ Количество должно быть не меньше 1.")
            return
        await state.update_data(message_count=count)
        await state.set_state(SchedulerStates.enter_attack_delay)
        await message.answer(f"Введите задержку в секундах (мин: {config.MIN_DELAY_FOR_ATTACK}).")
    except (ValueError, TypeError):
        await message.reply("❌ Введите корректное число.")


@router.message(SchedulerStates.enter_attack_delay)
async def schedule_attack_delay_received(message: Message, state: FSMContext):
    try:
        delay = float(message.text.strip().replace(',', '.'))
        if delay < config.MIN_DELAY_FOR_ATTACK:
            await message.reply(f"❌ Минимальная задержка - {config.MIN_DELAY_FOR_ATTACK} сек.")
            return
        await state.update_data(attack_delay=delay)
        await state.set_state(SchedulerStates.enter_attack_sessions)
        await message.answer("Введите количество сессий для задачи атаки.\nОтправьте '<b>all</b>' для использования всех доступных сессий.")
    except (ValueError, TypeError):
        await message.reply("❌ Введите корректное число.")


@router.message(SchedulerStates.enter_attack_sessions)
async def schedule_attack_sessions_received(message: Message, state: FSMContext):
    session_input = message.text.strip().lower()
    session_limit = None
    if session_input != 'all':
        try:
            session_limit = int(session_input)
            if session_limit <= 0:
                await message.reply("❌ Количество сессий должно быть положительным числом.")
                return
        except ValueError:
            await message.reply("❌ Введите число или 'all'.")
            return

    if not scheduler_manager:
        await message.answer("❌ Планировщик неактивен. Невозможно создать задачу.", reply_markup=settings_keyboard())
        await state.clear()
        return

    data = await state.get_data()
    user_id = message.from_user.id

    # Get user's current AI settings to pass to the task
    ai_settings = await db_manager.get_ai_settings(user_id)

    task_params = {
        'session_limit': session_limit,
        'attack_mode': data.get('attack_mode'),
        'target_nickname': data.get('target_nickname'),
        'message_count': data.get('message_count'),
        'attack_delay': data.get('attack_delay'),
        'use_ai': ai_settings.get('enabled', False)  # Use current AI setting
    }

    await scheduler_manager.add_task(
        user_id=user_id, task_type=data['task_type'],
        cron_expression=data['cron'], task_params=task_params
    )

    await message.answer("✅ Новая задача 'Атака в ЛС' успешно запланирована!", reply_markup=settings_keyboard())
    await state.clear()