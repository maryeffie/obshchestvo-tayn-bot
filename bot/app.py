"""Сборка приложения: связываем настройки, БД, бота и сервисы."""

from __future__ import annotations

from dataclasses import dataclass

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from .config import Settings
from .db import Database
from .prodamus import ProdamusClient
from .services.access import AccessService
from .services.payments import PaymentProcessor
from .services.subscriptions import SubscriptionService
from .services.tasks import ScheduledTasks


@dataclass
class App:
    settings: Settings
    db: Database
    bot: Bot
    prodamus: ProdamusClient
    access: AccessService
    payments: PaymentProcessor
    subscriptions: SubscriptionService
    tasks: ScheduledTasks

    @classmethod
    async def build(cls, settings: Settings) -> "App":
        db = Database(settings.db_path)
        await db.connect()
        await db.init()

        bot = Bot(
            token=settings.bot_token,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        prodamus = ProdamusClient(settings)
        access = AccessService(bot, settings, db)
        payments = PaymentProcessor(settings, db, access, prodamus)
        subscriptions = SubscriptionService(settings, db, prodamus, access)
        tasks = ScheduledTasks(settings, db, access, payments)
        return cls(
            settings=settings,
            db=db,
            bot=bot,
            prodamus=prodamus,
            access=access,
            payments=payments,
            subscriptions=subscriptions,
            tasks=tasks,
        )

    async def shutdown(self) -> None:
        try:
            await self.bot.session.close()
        finally:
            await self.db.close()


__all__ = ["App"]
