"""Оплата из-за рубежа: бессрочная ссылка с конвертацией валюты."""

from __future__ import annotations

import asyncio
from urllib.parse import parse_qs, urlparse

from bot.handlers import user as user_handlers
from bot.utils import from_iso, is_active, utcnow

from .helpers import buttons_of, make_app
from .test_handlers import FakeCallback, FakeMessage, FakeState, _handler


def _params(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}


def test_paywall_offers_other_currency_with_universal_link() -> None:
    async def scenario() -> None:
        app = await make_app()
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

        # 2. кнопка ведёт на бессрочную ссылку Prodamus
        call = FakeCallback("cur:menu", FakeMessage(user_id=801), user_id=801)
        await _handler(router, "callback_query", "cb_currency_menu")(call)
        sent = call.message.answers[-1]
        assert "Тайны не знают границ" in sent["text"]
        url_button = sent["keyboard"].inline_keyboard[0][0]
        assert url_button.url.startswith("https://payform.ru/g7d0lTx/?")
        params = _params(url_button.url)
        assert params["customer_extra"] == "tg:801"      # по нему найдём человека
        assert params["order_id"].startswith("tg801-")
        assert "subscription" not in params              # разовый платёж, без автосписания
        # после оплаты Prodamus возвращает человека в чат с ботом
        assert params["urlReturn"] == "https://t.me/society_secrets_bot"
        # подпись не передаём: на бессрочной ссылке Prodamus её не принимает
        assert "signature" not in params

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
                "order_id": order_id,
                "payment_status": "success",
                "sum": "9",
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
        assert "9 €" in app.bot.last_to(111)["text"]
        stats = await app.db.stats()
        assert stats["revenue_total"] == 790

        # продление — снова та же бессрочная ссылка в валюте
        renew_url, renew_amount, _order, renew_currency = await app.payments.create_link_auto(802)
        assert "payform.ru/g7d0lTx" in renew_url
        assert renew_amount == 790 and renew_currency == "rub"

        await app.db.close()

    asyncio.run(scenario())
