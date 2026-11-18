"""Бесплатный режим: цепочка напоминаний о продлении (3 сообщения за период)."""

from __future__ import annotations

import asyncio
from datetime import timedelta

from bot.utils import from_iso, utcnow

from .helpers import buttons_of, checkout_destination, make_app


def _revive(app, tg_id: int, paid_until):
    async def run():
        await app.db.upsert_user(tg_id, f"user{tg_id}", "Тест")
        await app.db.activate(tg_id, paid_until, source="prodamus")

    return run()


def test_three_stage_reminder_chain() -> None:
    async def scenario() -> None:
        app = await make_app(grace_days=3)
        now = utcnow()

        # 1. до конца доступа 2 дня → «за 3 дня»
        await _revive(app, 701, now + timedelta(days=2))
        assert (await app.tasks.tick())["reminded"] == 1
        text = app.bot.last_to(701)["text"]
        assert "пришло время продлить" in text
        keyboard = app.bot.last_to(701)["keyboard"]
        buttons = buttons_of(keyboard)
        assert buttons == ["Продлить подписку", "Продлить в другой валюте", "Моя подписка"]
        # URL-кнопка перенаправляет к Prodamus без второго нажатия
        pay_button = keyboard.inline_keyboard[0][0]
        assert pay_button.url and pay_button.url.startswith("https://bot.example.com/checkout/")
        assert "order_id=tg701-" in await checkout_destination(app, pay_button.url)

        # повторно то же сообщение не приходит
        assert (await app.tasks.tick())["reminded"] == 0

        # 2. наступил день окончания → «сегодня последний день»
        await _revive(app, 701, now - timedelta(hours=1))
        assert (await app.tasks.tick())["reminded"] == 1
        assert "последний день Вашего пропуска" in app.bot.last_to(701)["text"]
        assert (await app.tasks.tick())["reminded"] == 0

        # 3. следующий день (в пределах отсрочки) → «вчера закончился»
        await _revive(app, 701, now - timedelta(days=1, hours=1))
        assert (await app.tasks.tick())["reminded"] == 1
        late = app.bot.last_to(701)["text"]
        assert "закончился" in late
        assert "Продлить подписку" in app.bot.last_to(701)["keyboard"].inline_keyboard[0][0].text

        # больше напоминаний в этом периоде нет
        assert (await app.tasks.tick())["reminded"] == 0
        # и доступ ещё не отобран — отсрочка 3 дня
        assert (await app.tasks.tick())["expired"] == 0

        await app.db.close()

    asyncio.run(scenario())


def test_no_reminder_after_deadline_only_kick() -> None:
    async def scenario() -> None:
        app = await make_app(grace_days=1)
        # доступ закончился 5 дней назад — напоминать поздно
        await _revive(app, 702, utcnow() - timedelta(days=5))
        result = await app.tasks.tick()
        assert result["reminded"] == 0
        assert result["expired"] == 1
        assert any("перестал действовать" in t for t in app.bot.texts_to(702))
        await app.db.close()

    asyncio.run(scenario())


def test_reminder_stage_resets_after_payment() -> None:
    async def scenario() -> None:
        app = await make_app(grace_days=3)
        await _revive(app, 703, utcnow() + timedelta(days=2))
        assert (await app.tasks.tick())["reminded"] == 1
        user = await app.db.get_user(703)
        assert user["reminder_stage"] == 1

        # человек продлил — оплата пришла по вебхуку
        _url, _amount, order_id, _d = await app.payments.create_payment_link(703)
        await app.payments.handle(
            {
                "order_id": "prodamus-" + order_id,
                "order_num": order_id,
                "payment_status": "success",
                "sum": "790",
                "customer_extra": "tg:703",
                "subscription": "4242",
            }
        )
        user = await app.db.get_user(703)
        assert user["reminder_stage"] == 0
        until = from_iso(user["paid_until"])
        # 2 дня остатка + 30 новых; сравниваем с запасом, чтобы не ловить границу суток
        assert until is not None and until - utcnow() > timedelta(days=31, hours=20)

        # в новом периоде напоминания снова возможны
        await app.db.activate(703, utcnow() + timedelta(days=2), source="prodamus")
        assert (await app.tasks.tick())["reminded"] == 1

        await app.db.close()

    asyncio.run(scenario())


def test_cancelled_user_gets_no_reminders_at_all() -> None:
    async def scenario() -> None:
        app = await make_app(grace_days=3)
        await _revive(app, 704, utcnow() + timedelta(days=2))
        await app.db.set_auto_renew(704, False, cancelled=True)
        assert (await app.tasks.tick())["reminded"] == 0
        await app.db.close()

    asyncio.run(scenario())


def test_foreign_reminder_and_manual_grant_create_no_foreign_orders() -> None:
    async def scenario() -> None:
        app = await make_app(prodamus_foreign_url=None, prodamus_foreign_dynamic=True)
        now = utcnow()
        await _revive(app, 705, now + timedelta(days=2))
        await app.db.set_last_currency(705, "eur")
        await app.db.set_auto_renew(705, False, cancelled=False)
        # Сброс стадии: реальные старые иностранные платежи ставят её в 0.
        await app.db.set_reminder_stage(705, 0, (await app.db.get_user(705))["paid_until"])
        assert await app.tasks.send_renewal_reminders() == 1
        reminder = app.bot.last_to(705)
        assert "персональную ссылку" in reminder["text"]
        assert reminder["keyboard"].inline_keyboard[0][0].callback_data == "join"
        assert await app.db._fetchall("SELECT * FROM orders") == []

        # Явно отменившим подписку напоминания не отправляются.
        await _revive(app, 706, now + timedelta(days=2))
        await app.db.set_last_currency(706, "eur")
        await app.db.set_auto_renew(706, False, cancelled=True)
        assert await app.tasks.send_renewal_reminders() == 0

        await app.db.upsert_user(707)
        await app.db.activate(707, now + timedelta(days=2), source="manual")
        assert await app.tasks.send_renewal_reminders() == 1
        assert "персональную ссылку" in app.bot.last_to(707)["text"]
        assert await app.db._fetchall("SELECT * FROM orders") == []
        await app.db.close()
    asyncio.run(scenario())
