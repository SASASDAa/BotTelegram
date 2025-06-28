# bot/middlewares.py
import logging
from typing import Any, Awaitable, Callable, Dict, Optional, Union

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from bot.database.db_manager import db_manager
from bot.keyboards import maintenance_keyboard, shop_keyboard

logger = logging.getLogger(__name__)

async def check_subscription(event: Union[Message, CallbackQuery]) -> bool:
    """
    Проверяет, есть ли у пользователя активная подписка.
    Если нет, отправляет уведомление и возвращает False.
    Возвращает True, если подписка активна.
    """
    user_id = event.from_user.id
    # Админы всегда имеют доступ
    role = await db_manager.get_user_role(user_id)
    if role in ['admin', 'super_admin']:
        return True

    sub_status = await db_manager.get_subscription_status(user_id)
    if sub_status['active']:
        return True

    # У пользователя нет подписки
    text = "❌ Доступ ограничен\n\nДля использования этой функции необходима активная подписка."
    if isinstance(event, Message):
        await event.answer(text, reply_markup=await shop_keyboard())
    elif isinstance(event, CallbackQuery):
        await event.answer(text, show_alert=True)
        # Отправляем новое сообщение в чат, так как меню может быть закрыто
        await event.message.answer(text, reply_markup=await shop_keyboard())
    return False

class AccessMiddleware(BaseMiddleware):
    def __init__(self, super_admin_id: int):
        self.super_admin_id = super_admin_id

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        logger.info(f"Middleware triggered for event {type(event).__name__} from user {data['event_from_user'].id}")

        user = data.get('event_from_user')
        if not user:
            logger.warning("Middleware SKIPPING: 'event_from_user' not found in data.")
            return await handler(event, data)

        # 1. Проверка на тех. работы.
        is_maintenance_str: Optional[str] = await db_manager.get_bot_setting("maintenance")
        logger.info(f"Middleware: User {user.id}, Maintenance mode value from DB: '{is_maintenance_str}'")

        if is_maintenance_str == "1":
            if user.id != self.super_admin_id:
                logger.info(f"Middleware: User {user.id} blocked by maintenance mode.")
                text = "🛠️ <b>Бот на технических работах.</b>\n\nПожалуйста, попробуйте позже. Мы скоро вернемся!"
                if isinstance(event, Message):
                    await event.answer(text, reply_markup=maintenance_keyboard())
                elif isinstance(event, CallbackQuery):
                    await event.answer(text, show_alert=True)
                    try:
                        await event.message.edit_reply_markup(reply_markup=None)
                    except Exception:
                        pass
                return
            else:
                logger.info(f"Middleware: Superadmin {user.id} bypassed maintenance mode.")

        # 2. Админы и суперадмин обходят проверку на бан.
        role = await db_manager.get_user_role(user.id)
        if role in ['admin', 'super_admin']:
            return await handler(event, data)

        # 3. Проверяем бан для обычных пользователей.
        sub_status = await db_manager.get_subscription_status(user.id)
        if sub_status.get('is_banned', False):
            text = "❌ Вы заблокированы администратором."
            if isinstance(event, Message):
                await event.answer(text)
            elif isinstance(event, CallbackQuery):
                await event.answer(text, show_alert=True)
            return

        # 4. Если все проверки пройдены, передаем управление дальше.
        # Проверка подписки будет в самих хэндлерах.
        return await handler(event, data)