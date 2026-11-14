"""Кнопка «Я оплатил — проверить»: сигнал админу и выдача доступа одной кнопкой."""

from __future__ import annotations

import asyncio

from bot.handlers import admin as admin_handlers
from bot.handlers import user as user_handlers
from bot.utils import from_iso, is_active, utcnow

from .helpers import make_app
from .test_handlers import FakeCallback, FakeMessage, FakeState, _buttons, _handler

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


def test_admin_fakepay_runs_full_payment_path_without_money() -> None:
    """Тестовая оплата через вебхук: деньги не нужны, доступ выдаётся."""

    async def scenario() -> None:
        # без публичного адреса бот прогоняет обработку напрямую
        app = await make_app(public_base_url="")
        router = admin_handlers.build_router(app)  # type: ignore[arg-type]

        message = FakeMessage("/fakepay 888", user_id=ADMIN_ID)
        await _handler(router, "message", "cmd_fakepay")(message)

        assert "Тестовый платёж" in message.last_text
        user = await app.db.get_user(888)
        assert is_active(user)
        assert len(app.bot.invites) >= 2

        await app.db.close()

    asyncio.run(scenario())


def test_pay1_is_available_to_any_user_and_respects_limit() -> None:
    """/pay1 доступна обычному пользователю: ссылка на 1 ₽ и лимит использований."""
    from bot.handlers import user as user_handlers

    async def scenario() -> None:
        app = await make_app(pay1_limit=1)
        router = user_handlers.build_router(app)  # type: ignore[arg-type]
        handler = _handler(router, "message", "cmd_pay1")

        # обычный пользователь, не админ
        message = FakeMessage("/pay1", user_id=555)
        await handler(message, FakeState())
        assert "Тестовый платёж за 1 ₽" in message.last_text
        url = message.answers[-1]["keyboard"].inline_keyboard[0][0].url
        assert "discount_value=789" in url
        assert "subscription=4242" in url
        assert any("Тестовая оплата за 1 ₽" in text for text in app.bot.texts_to(ADMIN_ID))

        # лимит исчерпан — второй ссылки не выдаём
        second = FakeMessage("/pay1", user_id=556)
        await handler(second, FakeState())
        assert "Оплатить" not in second.answers[-1]["text"]
        assert "закончились" in second.answers[-1]["text"]

        await app.db.close()

    asyncio.run(scenario())


def test_pay1_silent_when_disabled() -> None:
    from bot.handlers import user as user_handlers

    async def scenario() -> None:
        app = await make_app(pay1_enabled=False)
        router = user_handlers.build_router(app)  # type: ignore[arg-type]
        message = FakeMessage("/pay1", user_id=555)
        await _handler(router, "message", "cmd_pay1")(message, FakeState())
        assert "выключена" in message.last_text
        await app.db.close()

    asyncio.run(scenario())
