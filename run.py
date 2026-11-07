#!/usr/bin/env python3
"""Точка входа: long polling Telegram + HTTP-сервер для вебхуков Prodamus + планировщик.

Запуск:  python run.py
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from aiogram import Dispatcher
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, ErrorEvent
from aiohttp import web
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from bot.app import App
from bot.config import ConfigError, load_settings
from bot.handlers import admin, user
from bot.utils import setup_logging
from bot.webhook import create_webhook_app

log = logging.getLogger("club")

COMMANDS = [
    BotCommand(command="start", description="Вступить в «Общество тайн»"),
    BotCommand(command="status", description="Моя подписка"),
    BotCommand(command="help", description="Помощь"),
]


async def preflight(app: App) -> None:
    """Проверяем права бота в канале и чате — чтобы о проблеме узнали сразу."""
    settings = app.settings
    problems: list[str] = []

    for label, chat in (("канал", settings.channel_id), ("чат", settings.chat_id)):
        try:
            member = await app.bot.get_chat_member(chat_id=chat, user_id=app.bot.id)
        except TelegramAPIError as exc:
            problems.append(f"{label} <code>{chat}</code> недоступен: {exc}")
            continue
        status = str(getattr(member, "status", ""))
        if status not in {"administrator", "creator"}:
            problems.append(f"бот не администратор в {label}е <code>{chat}</code>")
            continue
        if getattr(member, "can_invite_users", None) is False:
            problems.append(
                f"в {label}е <code>{chat}</code> нет права «Приглашать пользователей»"
            )

    if not settings.is_recurrent:
        problems.append(
            "PRODAMUS_SUBSCRIPTION_ID не задан — бот работает в режиме "
            "<b>разовых платежей</b>, автосписаний не будет."
        )

    text = (
        "🤖 <b>Бот «Общество тайн» запущен</b>\n\n"
        f"Цена: {settings.price} {settings.currency.upper()}\n"
        f"Канал: <code>{settings.channel_id}</code>\n"
        f"Чат: <code>{settings.chat_id}</code>\n"
        f"Вебхук: <code>{settings.webhook_url}</code>\n"
        f"Рекуррент: {'да' if settings.is_recurrent else 'нет'}\n"
        "Панель управления: /admin"
    )
    if problems:
        text += "\n\n⚠️ <b>На что обратить внимание:</b>\n" + "\n".join(f"• {p}" for p in problems)
        log.warning("Проблемы при старте: %s", problems)

    await app.access.notify_admins(text)


def build_error_handler(app: App):
    async def on_error(event: ErrorEvent) -> bool:
        log.exception("Необработанная ошибка", exc_info=event.exception)
        await app.access.notify_admins(
            "⚠️ <b>Ошибка в боте</b>\n\n"
            f"<code>{type(event.exception).__name__}: {event.exception}</code>\n\n"
            "Подробности — в логах."
        )
        return True

    return on_error


async def main() -> None:
    settings = load_settings()
    setup_logging(settings.log_level)
    log.info("Запуск бота «Общество тайн»…")

    app = await App.build(settings)

    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.include_router(admin.build_router(app))
    dispatcher.include_router(user.build_router(app))
    dispatcher.errors.register(build_error_handler(app))

    # --- HTTP-сервер для вебхуков Prodamus -------------------------------
    runner = web.AppRunner(create_webhook_app(app))
    await runner.setup()
    site = web.TCPSite(runner, host=settings.webhook_host, port=settings.webhook_port)
    await site.start()
    log.info(
        "Вебхук слушает http://%s:%s%s",
        settings.webhook_host,
        settings.webhook_port,
        settings.webhook_path,
    )

    # --- планировщик: напоминания (день 27) и авто-кик (день 31) ---------
    scheduler = AsyncIOScheduler(timezone=settings.timezone)
    scheduler.add_job(
        app.tasks.tick,
        IntervalTrigger(minutes=10),
        id="club-tick",
        max_instances=1,
        coalesce=True,
        next_run_time=datetime.now(settings.timezone) + timedelta(seconds=30),
    )
    scheduler.start()

    try:
        await app.bot.set_my_commands(COMMANDS)
        await app.bot.delete_webhook(drop_pending_updates=False)
        await preflight(app)
        log.info("Слушаю Telegram (long polling)…")
        await dispatcher.start_polling(
            app.bot,
            allowed_updates=dispatcher.resolve_used_update_types(),
            handle_signals=True,
        )
    finally:
        log.info("Остановка…")
        scheduler.shutdown(wait=False)
        await runner.cleanup()
        await app.shutdown()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Остановлено вручную")
    except ConfigError as exc:
        logging.error("Ошибка конфигурации: %s", exc)
        raise SystemExit(1) from exc
