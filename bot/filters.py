# bot/filters.py
from aiogram.filters import Filter
from aiogram.types import Message, CallbackQuery

import config
from .database.db_manager import db_manager


class IsAdminFilter(Filter):
    async def __call__(self, event: Message | CallbackQuery) -> bool:
        role = await db_manager.get_user_role(event.from_user.id)
        return role in ['admin', 'super_admin']

class IsSuperAdminFilter(Filter):
    async def __call__(self, event: Message | CallbackQuery) -> bool:
        return event.from_user.id == config.SUPER_ADMIN_ID