"""Оплата в валюте: разовый счёт для покупателей из других стран."""

from __future__ import annotations

import asyncio
from urllib.parse import parse_qs, urlparse

from bot.handlers import user as user_handlers
from bot.utils import from_iso, is_active, utcnow

from .helpers import buttons_of, make_app
from .test_handlers import FakeCallback, FakeMessage, FakeState, _handler


def _params(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}


def test_paywall_offers_other_country_and_builds_currency_link() -> None:
    async def scenario() -> None:
        app = await make_app()
        await app.db.upsert_user(801, "anna", "Анна")
        router = user_handlers.build_router(app)  # type: ignore[arg-type]

        # 1. на странице оплаты есть кнопка для другой страны
        message = FakeMessage("/start", user_id=801)
        await _handler(router, "message", "cmd_start")(message, FakeState())
        await _handler(router, "callback_query", "cb_join")(
            FakeCallback("join", message, user_id=801), FakeState()
        )
        paywall = message.answers[-1]
        assert "Оплатить из другой страны" in buttons_of(paywall["keyboard"])

        # 2. меню валют
        call = FakeCallback("cur:menu", FakeMessage(user_id=801), user_id=801)
        await _handler(router, "callback_query", "cb_currency_menu")(call)
        menu = call.message.answers[-1]
        assert "Оплата из другой страны" in menu["text"]
        labels = buttons_of(menu["keyboard"])
        assert "Евро — 9 €" in labels
        assert "Доллары — 10 $" in labels

        # 3. ссылка в евро: разовая, валюта eur, без подписки
        pick = FakeCallback("cur:pay:eur", FakeMessage(user_id=801), user_id=801)
        await _handler(router, "callback_query", "cb_currency_pay")(pick)
        sent = pick.message.answers[-1]
        assert "9 €" in sent["text"]
        url = sent["keyboard"].inline_keyboard[0][0].url
        params = _params(url)
        assert params["currency"] == "eur"
        assert "subscription" not in params
        assert "9" in params["products[0][price]"] or "9" in str(params)

        await app.db.close()

    asyncio.run(scenario())


def test_foreign_payment_grants_month_without_autorenew() -> None:
    async def scenario() -> None:
        app = await make_app()
        await app.db.upsert_user(802, "john", "John")
        url, amount, order_id = await app.payments.create_foreign_link(802, "usd")
        assert amount == 10

        result = await app.payments.handle(
            {
                "order_id": order_id,
                "payment_status": "success",
                "sum": "10",
                "currency": "usd",
                "customer_extra": "tg:802",
            }
        )
        assert result == "paid"

        user = await app.db.get_user(802)
        assert is_active(user)
        assert int(user["auto_renew"]) == 0          # автопродления нет
        assert user["last_currency"] == "usd"
        until = from_iso(user["paid_until"])
        assert 29 <= (until - utcnow()).days <= 30
        assert len(app.bot.invites) == 2

        # админу показали сумму в валюте, а в статистике остались рубли
        admin_text = app.bot.last_to(111)["text"]
        assert "10 $" in admin_text
        stats = await app.db.stats()
        assert stats["revenue_total"] == 790

        # продление предлагается в той же валюте
        renew_url, renew_amount, _order, renew_currency = await app.payments.create_link_auto(802)
        assert renew_currency == "usd" and renew_amount == 10
        assert _params(renew_url)["currency"] == "usd"

        await app.db.close()

    asyncio.run(scenario())
