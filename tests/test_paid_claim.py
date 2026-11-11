"""Кнопка «Я оплатил — проверить»: сигнал админу и выдача доступа одной кнопкой."""

from __future__ import annotations

import asyncio

from bot.handlers import admin as admin_handlers
from bot.handlers import user as user_handlers
from bot.utils import from_iso, is_active, utcnow

from .helpers import make_app
from .test_handlers import FakeCallback, FakeMessage, _buttons, _handler

ADMIN_ID = 111


def test_user_can_report_payment_and_admin_grants_access() -> None:
    async def scenario() -> None:
        app = await make_app()
        await app.db.upsert_user(555, "user555", "Тест")

        user_router = user_handlers.build_router(app)  # type: ignore[arg-type]
        admin_router = admin_handlers.build_router(app)  # type: ignore[arg-type]

        # 1. участник нажал «Я оплатил — проверить»
        call = FakeCallback("paid:notify", FakeMessage(user_id=555), user_id=555)
        await _handler(user_router, "callback_query", "cb_paid_notify")(call)

        # 2. подтверждение участнику
        assert "Передали администратору" in call.message.answers[-1]["text"]

        # 3. админу пришло уведомление с кнопкой выдачи
        admin_msg = app.bot.last_to(ADMIN_ID)
        assert admin_msg is not None
        assert "сообщает об оплате" in admin_msg["text"]
        assert "Выдать доступ на 30 дней" in _buttons(admin_msg["keyboard"])

        # 4. админ нажал кнопку — доступ выдан, участнику ушли ссылки
        grant = FakeCallback(
            "adm:quickgrant:555", FakeMessage(user_id=ADMIN_ID), user_id=ADMIN_ID
        )
        await _handler(admin_router, "callback_query", "cb_quick_grant")(grant)

        assert "Доступ выдан" in grant.message.answers[-1]["text"]
        user = await app.db.get_user(555)
        assert is_active(user)
        assert from_iso(user["paid_until"]) > utcnow()
        # участнику ушли одноразовые ссылки в канал и чат
        assert len(app.bot.invites) >= 2
        assert app.bot.texts_to(555)

        await app.db.close()

    asyncio.run(scenario())


def test_admin_testpay_command_grants_access_without_prodamus() -> None:
    async def scenario() -> None:
        app = await make_app()
        router = admin_handlers.build_router(app)  # type: ignore[arg-type]

        message = FakeMessage("/testpay 777", user_id=ADMIN_ID)
        await _handler(router, "message", "cmd_testpay")(message)

        assert "Тестовая оплата прогнана" in message.last_text
        user = await app.db.get_user(777)
        assert is_active(user)
        assert len(app.bot.invites) >= 2
        assert app.bot.texts_to(777)

        await app.db.close()

    asyncio.run(scenario())
