"""Ручная валютная оплата и совместимость ранее созданных заказов."""

from __future__ import annotations

import asyncio

from bot.handlers import user as user_handlers
from bot.utils import from_iso, is_active, utcnow

from .helpers import buttons_of, make_app
from .test_handlers import FakeCallback, FakeMessage, FakeState, _handler


def test_paywall_offers_other_currency_with_universal_link() -> None:
    async def scenario() -> None:
        app = await make_app(prodamus_foreign_url=None, prodamus_foreign_dynamic=True)
        await app.db.upsert_user(801, "anna", "Анна")
        router = user_handlers.build_router(app)  # type: ignore[arg-type]

        # 1. на странице оплаты — кнопка «в другой валюте»
        message = FakeMessage("/start", user_id=801)
        await _handler(router, "message", "cmd_start")(message, FakeState())
        await _handler(router, "callback_query", "cb_join")(
            FakeCallback("join", message, user_id=801), FakeState()
        )
        paywall = message.answers[-1]
        assert "Оплатить в рублях 790 ₽" in buttons_of(paywall["keyboard"])
        assert "Оплатить в другой валюте" in buttons_of(paywall["keyboard"])

        # Валютная кнопка не создаёт заказ и открывает контакт Марины.
        before = await app.db._fetchall("SELECT * FROM orders")
        call = FakeCallback("cur:menu", FakeMessage(user_id=801), user_id=801)
        await _handler(router, "callback_query", "cb_currency_menu")(call)
        sent = call.message.answers[-1]
        assert sent["text"] == (
            "Тайны не знают границ!\n\n"
            "Вы выбрали оплату в иностранной валюте и это совсем не проблема!\n\n"
            "Чтобы оформить пропуск в «Общество тайн», просто напишите нам в Telegram и мы отправим Вам персональную ссылку для оплаты.\n\n"
            "До встречи по ту сторону тайн!"
        )
        url_button = sent["keyboard"].inline_keyboard[0][0]
        assert url_button.text == "Получить ссылку на оплату"
        assert url_button.url == "https://t.me/m/gavHrzZfNjFk"
        assert await app.db._fetchall("SELECT * FROM orders") == before

        # 3. со страницы валюты можно вернуться к выбору способа оплаты
        assert "Назад" in buttons_of(sent["keyboard"])

        await app.db.close()

    asyncio.run(scenario())


def test_foreign_payment_grants_month_without_autorenew() -> None:
    async def scenario() -> None:
        app = await make_app()
        await app.db.upsert_user(802, "john", "John")
        url, amount, order_id, currency = await app.payments.create_foreign_link(802)
        assert currency == "rub" and amount == 790
        assert "payform.ru/g7d0lTx" in url

        # Prodamus присылает платёж в валюте плательщика — 9 евро
        result = await app.payments.handle(
            {
                "order_id": "prodamus-" + order_id,
                "order_num": order_id,
                "payment_status": "success",
                "sum": "790",
                "currency_sum": "11.68",
                "currency": "eur",
                "customer_extra": "tg:802",
            }
        )
        assert result == "paid"

        user = await app.db.get_user(802)
        assert is_active(user)
        assert int(user["auto_renew"]) == 0          # автопродления нет
        assert user["last_currency"] == "eur"
        until = from_iso(user["paid_until"])
        assert 29 <= (until - utcnow()).days <= 30
        assert len(app.bot.invites) == 2

        # админу показали валюту, в статистике остались рубли
        assert "11.68 €" in app.bot.last_to(111)["text"]
        stats = await app.db.stats()
        assert stats["revenue_total"] == 790

        # Повтор старого webhook не продлевает доступ повторно.
        assert await app.payments.handle({
            "order_id": "prodamus-" + order_id, "order_num": order_id,
            "payment_status": "success", "sum": "790", "currency": "eur",
        }) == "duplicate"
        assert (await app.db.get_user(802))["paid_until"] == user["paid_until"]

        await app.db.close()

    asyncio.run(scenario())


def test_manual_grant_confirms_identity_and_status() -> None:
    from datetime import timedelta
    from bot.handlers import admin as admin_handlers

    async def scenario() -> None:
        app = await make_app()
        await app.db.upsert_user(809, "example", "Имя")
        admin = admin_handlers.build_router(app)  # type: ignore[arg-type]
        state = FakeState()
        msg = FakeMessage("@example", user_id=111)
        await _handler(admin, "message", "grant_user")(msg, state)
        assert "809" in msg.last_text and "Сверьте ID" in msg.last_text
        assert (await app.db.get_user(809))["status"] != "active"
        await _handler(admin, "message", "grant_days")(FakeMessage("30", 111), state)
        user = await app.db.get_user(809)
        first_end = from_iso(user["paid_until"])
        assert is_active(user) and user["access_source"] == "manual"
        assert len(app.bot.invites) == 2
        # Продление добавляется к оставшемуся периоду.
        await app.payments.grant_manually(809, 30)
        assert from_iso((await app.db.get_user(809))["paid_until"]) == first_end + timedelta(days=30)
        user_router = user_handlers.build_router(app)  # type: ignore[arg-type]
        status = FakeMessage("/status", user_id=809)
        await _handler(user_router, "message", "cmd_status")(status, FakeState())
        assert "Доступ выдан вручную" in status.last_text
        assert "уточнения автосписания" in status.last_text
        await app.db.close()

    asyncio.run(scenario())


def test_myid_lets_member_verify_identity() -> None:
    async def scenario() -> None:
        app = await make_app()
        router = user_handlers.build_router(app)  # type: ignore[arg-type]
        message = FakeMessage("/myid", user_id=810)
        await _handler(router, "message", "cmd_myid")(message)
        assert "<code>810</code>" in message.last_text
        assert (await app.db.get_user(810))["username"] == "user810"
        await app.db.close()

    asyncio.run(scenario())


def test_foreign_renewal_from_reminder_reaches_manual_contact() -> None:
    from datetime import timedelta

    async def scenario() -> None:
        app = await make_app(prodamus_foreign_url=None)
        await app.db.upsert_user(811, "user811", "Тест")
        await app.db.activate(811, utcnow() + timedelta(days=2), source="prodamus")
        await app.db.set_last_currency(811, "eur")
        await app.db.set_auto_renew(811, False, cancelled=False)
        await app.db.set_reminder_stage(811, 0, (await app.db.get_user(811))["paid_until"])
        assert await app.tasks.send_renewal_reminders() == 1
        router = user_handlers.build_router(app)  # type: ignore[arg-type]
        reminder = app.bot.last_to(811)
        join = reminder["keyboard"].inline_keyboard[0][0].callback_data
        msg = FakeMessage(user_id=811)
        await _handler(router, "callback_query", "cb_join")(FakeCallback(join, msg, 811), FakeState())
        assert "Оплатить в другой валюте" in buttons_of(msg.answers[-1]["keyboard"])
        before = await app.db._fetchall("SELECT * FROM orders")
        await _handler(router, "callback_query", "cb_currency_menu")(FakeCallback("cur:menu", msg, 811))
        assert msg.answers[-1]["keyboard"].inline_keyboard[0][0].url == "https://t.me/m/gavHrzZfNjFk"
        assert await app.db._fetchall("SELECT * FROM orders") == before
        assert len(before) == 1 and before[0]["currency"] == "rub"
        await app.db.close()

    asyncio.run(scenario())
