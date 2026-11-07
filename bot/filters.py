"""Фильтры aiogram."""

from __future__ import annotations

from aiogram.filters import BaseFilter
from aiogram.types import TelegramObject

from .config import Settings


class IsAdmin(BaseFilter):
    """Пропускает только ID из ADMIN_IDS."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def __call__(self, event: TelegramObject, **kwargs) -> bool:
        user = getattr(event, "from_user", None)
        return bool(user and self.settings.is_admin(user.id))


__all__ = ["IsAdmin"]
