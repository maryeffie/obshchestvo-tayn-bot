"""Бесплатный режим: цепочка напоминаний о продлении (3 сообщения за период)."""

from __future__ import annotations

import asyncio
from datetime import timedelta

from bot.utils import from_iso, utcnow

from .helpers import make_app


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
        buttons = [b.text for row in keyboard.inline_keyboard for b in row]
        assert buttons == ["Продлить на месяц — 790 ₽", "Моя подписка"]
        # первая кнопка — сразу на форму оплаты Prodamus, без лишнего шага
        pay_button = keyboard.inline_keyboard[0][0]
        assert pay_button.url and pay_button.url.startswith("https://")
        assert "order_id=tg701-" in pay_button.url

        # повторно то же сообщение не приходит
        assert (await app.tasks.tick())["reminded"] == 0

        # 2. наступил день окончания → «сегодня последний день»
        await _revive(app, 701, now - timedelta(hours=1))
        assert (await app.tasks.tick())["reminded"] == 1
        assert "Сегодня последний день" in app.bot.last_to(701)["text"]
        assert (await app.tasks.tick())["reminded"] == 0

        # 3. следующий день (в пределах отсрочки) → «вчера закончился»
        await _revive(app, 701, now - timedelta(days=1, hours=1))
        assert (await app.tasks.tick())["reminded"] == 1
        late = app.bot.last_to(701)["text"]
        assert "вчера закончился" in late
        assert "Продлить на месяц" in app.bot.last_to(701)["keyboard"].inline_keyboard[0][0].text

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
                "order_id": order_id,
                "payment_status": "success",
                "sum": "790",
                "customer_extra": "tg:703",
                "subscription": "4242",
            }
        )
        user = await app.db.get_user(703)
        assert user["reminder_stage"] == 0
        until = from_iso(user["paid_until"])
        assert until is not None and (until - utcnow()).days >= 32

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
