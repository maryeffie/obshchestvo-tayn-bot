"""Сценарные тесты: оплата, продление, напоминание, авто-кик, промокоды."""

from __future__ import annotations

import asyncio
import time
from datetime import timedelta

from bot.prodamus import sign_payload
from bot.utils import from_iso, utcnow

from .helpers import make_app


def _success_payload(order_id: str, tg_id: int, amount: int = 790) -> dict:
    return {
        "order_id": order_id,
        "payment_status": "success",
        "payment_status_description": "Оплачено",
        "sum": f"{amount}.00",
        "customer_extra": f"tg:{tg_id}",
        "customer_phone": "+79990001122",
        "subscription": "4242",
    }


# ------------------------------------------------------------------ оплата

def test_paid_webhook_grants_access() -> None:
    async def scenario() -> None:
        app = await make_app()
        bot = app.bot
        await app.db.upsert_user(555, "marina_test", "Марина")

        _url, amount, order_id, discount = await app.payments.create_payment_link(555)
        assert amount == 790 and discount == 0

        result = await app.payments.handle(_success_payload(order_id, 555))
        assert result == "paid"

        user = await app.db.get_user(555)
        assert user is not None
        assert user["status"] == "active"
        assert user["access_source"] == "prodamus"
        assert user["phone"] == "+79990001122"
        until = from_iso(user["paid_until"])
        assert until is not None
        assert 29 <= (until - utcnow()).days <= 30

        # две одноразовые ссылки: в канал и в чат
        assert len(bot.invites) == 2
        assert {i["chat_id"] for i in bot.invites} == {
            app.settings.channel_id,
            app.settings.chat_id,
        }
        assert all(i["member_limit"] == 1 for i in bot.invites)
        assert all(i["expire_date"] > time.time() + 23 * 3600 for i in bot.invites)

        user_texts = bot.texts_to(555)
        assert any("Поздравляем" in text for text in user_texts)
        last = bot.last_to(555)
        buttons = [
            button.text
            for row in last["keyboard"].inline_keyboard
            for button in row
        ]
        assert buttons == ["Перейти в закрытый канал", "Перейти в чат обсуждений"]

        assert any("Новая оплата" in text for text in bot.texts_to(111))
        order = await app.db.get_order(order_id)
        assert order is not None and order["status"] == "paid"

        await app.db.close()

    asyncio.run(scenario())


def test_duplicate_webhook_is_ignored() -> None:
    async def scenario() -> None:
        app = await make_app()
        await app.db.upsert_user(556, "user556", "Тест")
        _url, _amount, order_id, _d = await app.payments.create_payment_link(556)

        assert await app.payments.handle(_success_payload(order_id, 556)) == "paid"
        invites_after_first = len(app.bot.invites)
        assert await app.payments.handle(_success_payload(order_id, 556)) == "duplicate"
        assert len(app.bot.invites) == invites_after_first

        user = await app.db.get_user(556)
        until = from_iso(user["paid_until"])
        assert until is not None and (until - utcnow()).days <= 30

        await app.db.close()

    asyncio.run(scenario())


def test_renewal_extends_existing_period() -> None:
    async def scenario() -> None:
        app = await make_app()
        await app.db.upsert_user(557, "user557", "Тест")
        await app.db.activate(557, utcnow() + timedelta(days=5), source="prodamus")

        _url, _amount, order_id, _d = await app.payments.create_payment_link(557)
        result = await app.payments.handle(_success_payload(order_id, 557))
        assert result == "renewed"

        user = await app.db.get_user(557)
        until = from_iso(user["paid_until"])
        assert until is not None
        assert 34 <= (until - utcnow()).days <= 35
        assert any("продлён до" in text for text in app.bot.texts_to(557))

        await app.db.close()

    asyncio.run(scenario())


def test_failed_payment_sends_retry_button() -> None:
    async def scenario() -> None:
        app = await make_app()
        await app.db.upsert_user(558, "user558", "Тест")
        _url, _amount, order_id, _d = await app.payments.create_payment_link(558)

        result = await app.payments.handle(
            {
                "order_id": order_id,
                "payment_status": "fail",
                "payment_status_description": "Недостаточно средств",
                "sum": "790",
                "customer_extra": "tg:558",
            }
        )
        assert result == "failed"
        last = app.bot.last_to(558)
        assert "Небольшая заминка" in last["text"]
        buttons = [b.text for row in last["keyboard"].inline_keyboard for b in row]
        assert buttons == ["Повторить оплату"]
        order = await app.db.get_order(order_id)
        assert order["status"] == "failed"
        user = await app.db.get_user(558)
        assert user["status"] == "new"

        await app.db.close()

    asyncio.run(scenario())


def test_payment_without_known_order_found_by_phone() -> None:
    """Продление без order_id: пользователя находим по телефону из первого платежа."""

    async def scenario() -> None:
        app = await make_app()
        await app.db.upsert_user(559, "user559", "Тест")
        await app.db.save_contacts(559, "+79995554433", None)
        await app.db.activate(559, utcnow() + timedelta(days=1), source="prodamus")

        result = await app.payments.handle(
            {
                "payment_status": "success",
                "sum": "790",
                "customer_phone": "+79995554433",
                "subscription": "4242",
            }
        )
        assert result == "renewed"
        user = await app.db.get_user(559)
        until = from_iso(user["paid_until"])
        assert until is not None and (until - utcnow()).days >= 30

        await app.db.close()

    asyncio.run(scenario())


# --------------------------------------------------------------- промокоды

def test_promo_discount_applied_and_counted() -> None:
    async def scenario() -> None:
        app = await make_app()
        await app.db.upsert_user(560, "user560", "Тест")
        await app.db.create_promo("TAJNA20", 20, max_uses=1)

        ok, _reason = await app.db.promo_is_valid("tajna20")
        assert ok

        url, amount, order_id, discount = await app.payments.create_payment_link(
            560, promo_code="TAJNA20"
        )
        assert amount == 632
        assert discount == 158
        assert "discount_value=158" in url

        await app.payments.handle(_success_payload(order_id, 560, amount=632))
        promo = await app.db.get_promo("TAJNA20")
        assert promo["used_count"] == 1
        ok, reason = await app.db.promo_is_valid("TAJNA20")
        assert not ok and "исчерпан" in reason

        await app.db.close()

    asyncio.run(scenario())


# --------------------------------------------- напоминание и авто-кик

def test_reminder_then_auto_kick() -> None:
    async def scenario() -> None:
        app = await make_app()
        await app.db.upsert_user(561, "user561", "Тест")
        # 28-й день: до конца доступа меньше 3 дней
        await app.db.activate(561, utcnow() + timedelta(days=2), source="prodamus")

        first = await app.tasks.tick()
        assert first["reminded"] == 1
        assert any(
            "пришло время продлить" in text for text in app.bot.texts_to(561)
        )

        # повторный тик не спамит
        second = await app.tasks.tick()
        assert second["reminded"] == 0

        # доступ закончился 2 дня назад — с учётом отсрочки пора исключать
        await app.db.activate(561, utcnow() - timedelta(days=2), source="prodamus")
        app.bot.clear()
        third = await app.tasks.tick()
        assert third["expired"] == 1

        banned_chats = {chat for chat, _uid in app.bot.banned}
        assert banned_chats == {app.settings.channel_id, app.settings.chat_id}
        assert all(uid == 561 for _chat, uid in app.bot.banned)
        # после исключения снимаем бан, чтобы человек мог вернуться
        assert len(app.bot.unbanned) == 2
        last = app.bot.last_to(561)
        assert "перестал действовать" in last["text"]
        buttons = [b.text for row in last["keyboard"].inline_keyboard for b in row]
        assert buttons == ["Вернуться в клуб"]

        user = await app.db.get_user(561)
        assert user["status"] == "expired"

        # ещё один тик — никого не трогаем
        assert (await app.tasks.tick())["expired"] == 0

        await app.db.close()

    asyncio.run(scenario())


def test_grace_period_respected() -> None:
    async def scenario() -> None:
        app = await make_app()
        await app.db.upsert_user(562, "user562", "Тест")
        # доступ истёк 12 часов назад — отсрочка 1 день ещё не прошла
        await app.db.activate(562, utcnow() - timedelta(hours=12), source="prodamus")
        result = await app.tasks.tick()
        assert result["expired"] == 0
        assert not app.bot.banned
        await app.db.close()

    asyncio.run(scenario())


# ------------------------------------------------------- ручная выдача

def test_manual_grant_sends_links() -> None:
    async def scenario() -> None:
        app = await make_app()
        ok, paid_until, error = await app.payments.grant_manually(900, days=5)
        assert ok and error is None
        user = await app.db.get_user(900)
        assert user["status"] == "active"
        assert user["access_source"] == "manual"
        assert 4 <= (from_iso(user["paid_until"]) - utcnow()).days <= 5
        assert len(app.bot.invites) == 2
        assert any("Поздравляем" in t or "пропуск" in t for t in app.bot.texts_to(900))
        await app.db.close()

    asyncio.run(scenario())


def test_blocked_user_does_not_break_payment() -> None:
    async def scenario() -> None:
        app = await make_app()
        app.bot.blocked_users.add(901)
        await app.db.upsert_user(901, "blocked", "Тест")
        _url, _amount, order_id, _d = await app.payments.create_payment_link(901)
        assert await app.payments.handle(_success_payload(order_id, 901)) == "paid"
        user = await app.db.get_user(901)
        assert user["status"] == "active"
        await app.db.close()

    asyncio.run(scenario())


def test_signature_of_webhook_payload() -> None:
    """Подпись, посчитанная для payload, проходит проверку клиента."""
    async def scenario() -> None:
        app = await make_app()
        payload = _success_payload("tg1-abc", 1)
        signature = sign_payload(payload, app.settings.prodamus_secret)
        assert app.prodamus.verify_request(payload, {"Sign": signature, "Sign-Algorithm": "sha256"})
        assert not app.prodamus.verify_request(payload, {"Sign": "bad"})
        await app.db.close()

    asyncio.run(scenario())
