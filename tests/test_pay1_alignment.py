"""Одинаковое создание тестового и рабочего заказа; точная привязка webhook."""
import asyncio
from urllib.parse import parse_qs, urlsplit

from bot.handlers import user as user_handlers
from .helpers import make_app
from .test_handlers import FakeMessage, FakeState, _handler


def params(url):
    return {key: values[0] for key, values in parse_qs(urlsplit(url).query).items()}


def test_pay1_and_normal_use_same_builder_and_share_delivery_path():
    async def scenario():
        app = await make_app()
        try:
            user_id = 7001
            msg = FakeMessage("/pay1", user_id=user_id)
            router = user_handlers.build_router(app)
            await _handler(router, "message", "cmd_pay1")(msg, FakeState())
            test_url = msg.answers[-1]["keyboard"].inline_keyboard[0][0].url
            normal_url, _, normal_order, _ = await app.payments.create_payment_link(user_id)
            t, n = params(test_url), params(normal_url)
            test_order = t["order_id"]
            assert t["subscription"] == n["subscription"]
            assert t["do"] == n["do"] == "pay"
            assert t["customer_extra"] == "order:" + test_order
            assert n["customer_extra"] == "order:" + normal_order
            assert t["discount_value"] == "789" and "discount_value" not in n
            assert (await app.db.get_order(test_order))["amount"] == 1
            for payment_id, order, amount in (("80001", test_order, "1"), ("80002", normal_order, "790")):
                payload = {"order_id": payment_id, "order_num": order,
                           "payment_status": "success", "sum": amount}
                assert await app.payments.handle(payload) in {"paid", "renewed"}
                until = (await app.db.get_user(user_id))["paid_until"]
                assert await app.payments.handle(payload) == "duplicate"
                assert (await app.db.get_user(user_id))["paid_until"] == until
            assert len(app.bot.invites) == 2  # первая оплата; продление без новых приглашений
        finally:
            await app.db.close()
    asyncio.run(scenario())


def test_exact_order_marker_is_only_backup_not_a_guess():
    async def scenario():
        app = await make_app()
        try:
            await app.db.upsert_user(7002)
            _, _, order, _ = await app.payments.create_payment_link(7002)
            payload = {"order_id": "80003", "order_num": "", "payment_status": "success", "sum": "790"}
            assert await app.payments.handle({**payload, "customer_extra": "tg:7002"}) == "unknown-user"
            assert await app.payments.handle({**payload, "customer_extra": "order:" + order + " text"}) == "unknown-user"
            assert await app.payments.handle({**payload, "order_num": "other", "customer_extra": "order:" + order}) == "unknown-user"
            assert await app.payments.handle({**payload, "customer_extra": "order:" + order}) == "paid"
            assert await app.payments.handle({**payload, "customer_extra": "order:" + order}) == "duplicate"
        finally:
            await app.db.close()
    asyncio.run(scenario())


def test_dynamic_foreign_uses_same_builder_is_one_time_and_opt_in():
    async def scenario():
        app = await make_app(prodamus_foreign_dynamic=True)
        try:
            await app.db.upsert_user(7003)
            url, amount, order, currency = await app.payments.create_foreign_link(7003)
            p = params(url)
            assert url.startswith(app.settings.prodamus_form_url + "?")
            assert p["order_id"] == order
            assert p["customer_extra"] == "order:" + order
            assert p["products[0][price]"] == "790"
            assert p["do"] == "pay" and "subscription" not in p
            assert "signature" in p
            assert amount == 790 and currency == "rub"
            row = await app.db.get_order(order)
            assert row["currency"] == "foreign" and row["is_recurring"] == 0
            payload = {"order_id": "80004", "order_num": order,
                       "payment_status": "success", "sum": "790", "currency": "usd", "currency_sum": "11.68"}
            assert await app.payments.handle(payload) == "paid"
            assert await app.payments.handle(payload) == "duplicate"
            assert (await app.db.get_user(7003))["auto_renew"] == 0
            assert "11.68 $" in app.bot.last_to(111)["text"]
        finally:
            await app.db.close()
    asyncio.run(scenario())


def test_signed_http_fallback_marker_and_invalid_signature():
    from aiohttp import ClientSession, web
    from bot.prodamus import sign_payload
    from bot.webhook import create_webhook_app

    async def scenario():
        app = await make_app()
        runner = web.AppRunner(create_webhook_app(app))
        try:
            await app.db.upsert_user(7004)
            _, _, order, _ = await app.payments.create_payment_link(7004)
            payload = {"order_id": "80005", "order_num": "", "payment_status": "success",
                       "sum": "790", "customer_extra": "order:" + order}
            await runner.setup()
            site = web.TCPSite(runner, "127.0.0.1", 0)
            await site.start()
            url = f"http://127.0.0.1:{runner.addresses[0][1]}{app.settings.webhook_path}"
            async with ClientSession() as session:
                async with session.post(url, data=payload, headers={"Sign": "invalid"}) as resp:
                    assert resp.status == 403
                assert (await app.db.get_user(7004))["status"] == "new"
                signature = sign_payload(payload, app.settings.prodamus_secret)
                async with session.post(url, data=payload, headers={"Sign": signature}) as resp:
                    assert resp.status == 200
                assert (await app.db.get_user(7004))["status"] == "active"
                until = (await app.db.get_user(7004))["paid_until"]
                async with session.post(url, data=payload, headers={"Sign": signature}) as resp:
                    assert resp.status == 200
                assert (await app.db.get_user(7004))["paid_until"] == until
                assert len(app.bot.invites) == 2
        finally:
            await runner.cleanup()
            await app.db.close()
    asyncio.run(scenario())


def test_foreign_off_remains_disabled_even_in_dynamic_mode():
    async def scenario():
        app = await make_app(prodamus_foreign_dynamic=True, prodamus_foreign_url=None)
        try:
            await app.db.upsert_user(7005)
            try:
                await app.payments.create_foreign_link(7005)
                assert False, "foreign payments must remain disabled"
            except ValueError:
                pass
            assert await app.db.find_pending_order(7005) is None
        finally:
            await app.db.close()
    asyncio.run(scenario())
