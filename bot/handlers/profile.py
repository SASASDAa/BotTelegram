# bot/handlers/profile.py
from datetime import datetime

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.database.db_manager import db_manager
from bot.keyboards import profile_inline_keyboard
from bot.states import ProfileStates

router = Router()

@router.message(F.text.in_({"👤 Профиль", "/profile"}))
async def profile_command(message: Message):
    user_id = message.from_user.id
    sub_status = await db_manager.get_subscription_status(user_id)
    
    text = f"<b>👤 Ваш профиль</b>\n\n"
    text += f"▫️ <b>ID:</b> <code>{user_id}</code>\n"
    
    if sub_status['active']:
        expires_at = sub_status['expires_at']
        expires_at_str = expires_at.strftime('%Y-%m-%d %H:%M')
        remaining = expires_at - datetime.now()
        days_rem = remaining.days
        hours_rem = remaining.seconds // 3600

        text += f"▫️ <b>Статус подписки:</b> Активна ✅\n"
        text += f"▫️ <b>Действует до:</b> {expires_at_str}\n"
        text += f"▫️ <b>Осталось:</b> {days_rem} д. {hours_rem} ч."
    else:
        text += f"▫️ <b>Статус подписки:</b> Неактивна ❌\n\n"
        text += "Для использования функций бота необходима активная подписка."
        
    await message.answer(text, reply_markup=await profile_inline_keyboard())

@router.callback_query(F.data == "activate_promo_code")
async def activate_promo_start(query: CallbackQuery, state: FSMContext):
    await state.set_state(ProfileStates.enter_promo_code)
    await query.message.answer("Введите промокод для активации подписки.\n/cancel для отмены.")
    await query.answer()

@router.message(ProfileStates.enter_promo_code)
async def activate_promo_received(message: Message, state: FSMContext):
    promo_code_str = message.text.strip()
    user_id = message.from_user.id
    
    promo_details = await db_manager.get_promo_code_details(promo_code_str)
    
    if not promo_details:
        await message.reply("❌ Промокод не найден. Проверьте правильность ввода.")
        await state.clear()
        return
        
    already_activated = await db_manager.has_user_activated_code(promo_code_str, user_id)
    if already_activated:
        await message.reply("❌ Вы уже использовали этот промокод.")
        await state.clear()
        return

    max_activations = promo_details['max_activations']
    current_activations = promo_details['current_activations']
    if max_activations != 0 and current_activations >= max_activations:
        await message.reply("❌ Лимит активаций этого промокода исчерпан.")
        await state.clear()
        return

    duration_days = promo_details['duration_days']
    
    await db_manager.activate_promo_code(promo_code_str, user_id)
    new_expiry_date = await db_manager.grant_subscription(user_id, duration_days)
    expiry_str = new_expiry_date.strftime('%Y-%m-%d %H:%M')
    
    await message.answer(
        f"✅ Промокод успешно активирован!\n\nВам начислено {duration_days} дней подписки.\nНовая дата окончания: {expiry_str}"
    )
    await state.clear()