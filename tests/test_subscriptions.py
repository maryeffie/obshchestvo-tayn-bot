"""Отмена подписки: сервис, кнопка в боте, поведение планировщика и вебхуки."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace

from bot.handlers import admin as admin_handlers
from bot.handlers import user as user_handlers
from bot.utils import from_iso, utcnow

from .helpers import make_app
from .test_handlers import FakeCallback, FakeMessage, FakeState, _buttons, _handler


def _active_user(app, tg_id: int, days: int = 20, phone: str | None = None):
    async def run():
        await app.db.upsert_user(tg_id, f"user{tg_id}", "Тест")
        if phone:
            await app.db.save_contacts(tg_id, phone, None)
        await app.db.activate(tg_id, utcnow() + timedelta(days=days), source="prodamus")
        return await app.db.get_user(tg_id)

    return run


# ---------------------------------------------------------------- сервис

def test_cancel_turns_off_autorenew_and_keeps_access() -> None:
    async def scenario() -> None:
        app = await make_app()
        await app.db.upsert_user(601, "user601", "Тест")
        await app.db.activate(601, utcnow() + timedelta(days=20), source="prodamus")
        before = await app.db.get_user(601)

        outcome = await app.subscriptions.cancel(601)

        assert outcome.local_ok and outcome.remote_ok
        assert app.remote_calls and app.remote_calls[0]["as_manager"] is False
        assert app.remote_calls[0]["active"] is False
        assert app.remote_calls[0]["tg_user_id"] == 601

        after = await app.db.get_user(601)
        assert after["auto_renew"] == 0
        assert after["cancelled_at"] is not None
        # доступ не отбираем — только автосписание
        assert after["status"] == "active"
        assert after["paid_until"] == before["paid_until"]
        assert any("отменил подписку" in t for t in app.bot.texts_to(111))

        await app.db.close()

    asyncio.run(scenario())


def test_cancel_without_prodamus_confirmation_still_marks_local() -> None:
    async def scenario() -> None:
        app = await make_app()
        await app.db.upsert_user(602, "user602", "Тест")
        await app.db.activate(602, utcnow() + timedelta(days=10), source="prodamus")
        app.remote_ok = False

        outcome = await app.subscriptions.cancel(602)

        assert outcome.local_ok and not outcome.remote_ok
        assert outcome.error
        user = await app.db.get_user(602)
        assert user["auto_renew"] == 0
        admin_text = app.bot.texts_to(111)[-1]
        assert "не подтвердил отключение" in admin_text
        assert "вручную" in admin_text

        await app.db.close()

    asyncio.run(scenario())


def test_admin_cancel_uses_manager_mode() -> None:
    async def scenario() -> None:
        app = await make_app()
        await app.db.upsert_user(603, "user603", "Тест")
        await app.db.activate(603, utcnow() + timedelta(days=5), source="prodamus")

        await app.subscriptions.cancel(603, as_manager=True)

        assert app.remote_calls[0]["as_manager"] is True
        assert any("администратором" in t for t in app.bot.texts_to(111))
        user = await app.db.get_user(603)
        assert user["auto_renew"] == 0

        await app.db.close()

    asyncio.run(scenario())


# ------------------------------------------------------- планировщик

def test_cancelled_user_gets_no_reminder_and_is_kicked_on_time() -> None:
    async def scenario() -> None:
        app = await make_app()
        await app.db.upsert_user(604, "user604", "Тест")
        # до конца доступа 2 дня — обычному пользователю пришло бы напоминание
        await app.db.activate(604, utcnow() + timedelta(days=2), source="prodamus")
        await app.db.set_auto_renew(604, False, cancelled=True)

        result = await app.tasks.tick()
        assert result["reminded"] == 0
        assert not any("пришло время продлить" in t for t in app.bot.texts_to(604))

        # период закончился 10 минут назад: без отсрочки уже исключаем
        await app.db.activate(604, utcnow() - timedelta(minutes=10), source="prodamus")
        await app.db.set_auto_renew(604, False, cancelled=True)
        result = await app.tasks.tick()
        assert result["expired"] == 1
        assert {chat for chat, _uid in app.bot.banned} == {
            app.settings.channel_id,
            app.settings.chat_id,
        }

        await app.db.close()

    asyncio.run(scenario())


# ---------------------------------------------------------- хендлеры

def test_cancel_button_flow() -> None:
    async def scenario() -> None:
        app = await make_app()
        await app.db.upsert_user(605, "user605", "Тест")
        await app.db.activate(605, utcnow() + timedelta(days=15), source="prodamus")
        router = user_handlers.build_router(app)  # type: ignore[arg-type]

        # 1. «Моя подписка» — есть кнопка отмены
        message = FakeMessage("/status", user_id=605)
        await _handler(router, "message", "cmd_status")(message, FakeState())
        assert "Отменить подписку" in _buttons(message.answers[-1]["keyboard"])

        # 2. нажали «Отменить» — просим подтверждение
        call = FakeCallback("sub:cancel", FakeMessage(user_id=605), user_id=605)
        await _handler(router, "callback_query", "cb_cancel")(call)
        confirm = call.message.answers[-1]
        assert "Отменить подписку?" in confirm["text"]
        assert "Да, отменить подписку" in _buttons(confirm["keyboard"])

        # 3. подтвердили — отмена
        call_yes = FakeCallback("sub:cancel_yes", FakeMessage(user_id=605), user_id=605)
        await _handler(router, "callback_query", "cb_cancel_yes")(call_yes)
        assert "подписка отменена" in call_yes.message.answers[-1]["text"]
        user = await app.db.get_user(605)
        assert user["auto_renew"] == 0

        # 4. в статусе больше нет кнопки отмены и написано «отменено»
        status = FakeMessage("/status", user_id=605)
        await _handler(router, "message", "cmd_status")(status, FakeState())
        assert "отменено" in status.last_text
        assert "Отменить подписку" not in _buttons(status.answers[-1]["keyboard"])

        await app.db.close()

    asyncio.run(scenario())


def test_cancel_declined_keeps_subscription() -> None:
    async def scenario() -> None:
        app = await make_app()
        await app.db.upsert_user(606, "user606", "Тест")
        await app.db.activate(606, utcnow() + timedelta(days=15), source="prodamus")
        router = user_handlers.build_router(app)  # type: ignore[arg-type]

        call = FakeCallback("sub:cancel_no", FakeMessage(user_id=606), user_id=606)
        await _handler(router, "callback_query", "cb_cancel_no")(call)

        user = await app.db.get_user(606)
        assert user["auto_renew"] == 1
        assert call.answered and "как есть" in call.answered[0]

        await app.db.close()

    asyncio.run(scenario())


def test_cannot_cancel_without_active_subscription() -> None:
    async def scenario() -> None:
        app = await make_app()
        await app.db.upsert_user(607, "user607", "Тест")
        router = user_handlers.build_router(app)  # type: ignore[arg-type]

        message = FakeMessage("/status", user_id=607)
        await _handler(router, "message", "cmd_status")(message, FakeState())
        assert "Отменить подписку" not in _buttons(message.answers[-1]["keyboard"])

        call = FakeCallback("sub:cancel", FakeMessage(user_id=607), user_id=607)
        await _handler(router, "callback_query", "cb_cancel")(call)
        assert call.answered  # всплывающая подсказка «отменять нечего»

        await app.db.close()

    asyncio.run(scenario())


def test_admin_cancel_subscription_flow() -> None:
    async def scenario() -> None:
        app = await make_app()
        await app.db.upsert_user(608, "user608", "Тест")
        await app.db.activate(608, utcnow() + timedelta(days=9), source="prodamus")
        router = admin_handlers.build_router(app)  # type: ignore[arg-type]
        state = FakeState()

        await _handler(router, "callback_query", "cb_cancel_sub")(
            FakeCallback("adm:cancel_sub", FakeMessage(user_id=111), user_id=111), state
        )
        assert state.state is not None

        message = FakeMessage("608", user_id=111)
        await _handler(router, "message", "cancel_sub_user")(message, state)
        assert "Автосписание отключено" in message.last_text
        assert app.remote_calls[0]["as_manager"] is True

        user = await app.db.get_user(608)
        assert user["auto_renew"] == 0

        await app.db.close()

    asyncio.run(scenario())


# ------------------------------------------------------------- вебхуки

def test_deactivation_webhook_turns_off_autorenew() -> None:
    async def scenario() -> None:
        app = await make_app()
        await app.db.upsert_user(609, "user609", "Тест")
        await app.db.activate(609, utcnow() + timedelta(days=12), source="prodamus")

        result = await app.payments.handle(
            {
                "type": "action",
                "action_code": "deactivation",
                "active": "0",
                "subscription": "4242",
                "customer_extra": "tg:609",
            }
        )

        assert result == "subscription-cancelled"
        user = await app.db.get_user(609)
        assert user["auto_renew"] == 0
        assert user["status"] == "active"  # доступ ещё действует
        assert any("Prodamus" in t for t in app.bot.texts_to(111))

        await app.db.close()

    asyncio.run(scenario())


def test_new_payment_reenables_autorenew() -> None:
    async def scenario() -> None:
        app = await make_app()
        await app.db.upsert_user(610, "user610", "Тест")
        await app.db.activate(610, utcnow() + timedelta(days=3), source="prodamus")
        await app.subscriptions.cancel(610)

        _url, _amount, order_id, _d = await app.payments.create_payment_link(610)
        await app.payments.handle(
            {
                "order_id": order_id,
                "payment_status": "success",
                "sum": "790",
                "customer_extra": "tg:610",
                "subscription": "4242",
            }
        )

        user = await app.db.get_user(610)
        assert user["auto_renew"] == 1
        assert user["cancelled_at"] is None
        until = from_iso(user["paid_until"])
        assert until is not None and (until - utcnow()).days >= 32

        await app.db.close()

    asyncio.run(scenario())


def test_subscription_result_dataclass_defaults() -> None:
    from bot.prodamus import SubscriptionResult

    result = SubscriptionResult(ok=True, message="success")
    assert result.remote is True and result.tried is None
    assert isinstance(SimpleNamespace(), object)


# ------------------------------------------- бесплатный режим (без автосписаний)

def test_manual_mode_cancel_button_works_without_prodamus() -> None:
    """Кнопка отмены обязана работать и когда автосписаний нет вовсе."""

    async def scenario() -> None:
        app = await make_app(prodamus_subscription_id=None)
        assert app.settings.is_recurrent is False
        await app.db.upsert_user(608, "user608", "Тест")
        await app.db.activate(608, utcnow() + timedelta(days=12), source="prodamus")
        router = user_handlers.build_router(app)  # type: ignore[arg-type]

        # 1. кнопка есть в «Моя подписка» и в сообщении о продлении
        status = FakeMessage("/status", user_id=608)
        await _handler(router, "message", "cmd_status")(status, FakeState())
        assert "Отменить подписку" in _buttons(status.answers[-1]["keyboard"])
        assert "вручную" in status.last_text

        # 2. подтверждение спрашиваем словами «не продлевать»
        call = FakeCallback("sub:cancel", FakeMessage(user_id=608), user_id=608)
        await _handler(router, "callback_query", "cb_cancel")(call)
        assert "Не продлевать подписку?" in call.message.answers[-1]["text"]

        # 3. подтвердили — отмена отмечена локально, в Prodamus не ходили
        call_yes = FakeCallback("sub:cancel_yes", FakeMessage(user_id=608), user_id=608)
        await _handler(router, "callback_query", "cb_cancel_yes")(call_yes)
        assert "продлевать не будем" in call_yes.message.answers[-1]["text"]
        user = await app.db.get_user(608)
        assert user["auto_renew"] == 0
        assert user["cancelled_at"] is not None
        assert app.remote_calls == []

        # 4. в статусе — «отменено» и никаких автосписаний
        status = FakeMessage("/status", user_id=608)
        await _handler(router, "message", "cmd_status")(status, FakeState())
        assert "отменено" in status.last_text
        assert "Отменить подписку" not in _buttons(status.answers[-1]["keyboard"])

        await app.db.close()

    asyncio.run(scenario())


def test_manual_mode_cancelled_user_gets_no_reminders() -> None:
    async def scenario() -> None:
        app = await make_app(prodamus_subscription_id=None)
        await app.db.upsert_user(609, "user609", "Тест")
        await app.db.activate(609, utcnow() + timedelta(days=2), source="prodamus")
        await app.db.set_auto_renew(609, False, cancelled=True)

        result = await app.tasks.tick()
        assert result["reminded"] == 0
        assert not any("продлить" in t.lower() for t in app.bot.texts_to(609))

        # день окончания наступил — исключаем ровно в срок, без отсрочки
        await app.db.activate(609, utcnow() - timedelta(minutes=5), source="prodamus")
        await app.db.set_auto_renew(609, False, cancelled=True)
        result = await app.tasks.tick()
        assert result["expired"] == 1
        assert {chat for chat, _uid in app.bot.banned} == {
            app.settings.channel_id,
            app.settings.chat_id,
        }

        await app.db.close()

    asyncio.run(scenario())
