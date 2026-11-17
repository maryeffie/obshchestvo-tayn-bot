"""Переход к оплате одним нажатием без ложного подтверждения платежа."""
import asyncio
import sqlite3
from urllib.parse import urlsplit

from aiohttp import ClientSession, web

from bot.db import Database, SCHEMA
from bot.handlers import user as user_handlers
from bot.webhook import create_webhook_app
from .helpers import make_app
from .test_handlers import FakeCallback, FakeMessage, FakeState, _handler


def test_payment_button_redirects_and_notifies_only_once():
    async def scenario():
        app = await make_app()
        runner = web.AppRunner(create_webhook_app(app), access_log=None)
        try:
            router = user_handlers.build_router(app)
            message = FakeMessage(user_id=8001)
            await _handler(router, "callback_query", "cb_join")(
                FakeCallback("join", message, user_id=8001), FakeState()
            )
            button = message.answers[-1]["keyboard"].inline_keyboard[0][0]
            assert button.url and button.callback_data is None
            path = urlsplit(button.url).path
            token = path.rsplit("/", 1)[-1]
            order = await app.db.get_order_by_checkout_token(token)
            assert order and order["tg_id"] == 8001
            assert not app.bot.texts_to(8001)
            assert not app.bot.invites
            await runner.setup()
            await web.TCPSite(runner, "127.0.0.1", 0).start()
            base = f"http://127.0.0.1:{runner.addresses[0][1]}"
            async with ClientSession() as session:
                async with session.head(base + path, allow_redirects=False) as response:
                    assert response.status == 405
                async with session.get(base + "/checkout/" + "0" * 32, allow_redirects=False) as response:
                    assert response.status == 404
                async with session.get(base + path, allow_redirects=False) as response:
                    assert response.status == 302
                    assert response.headers["Location"] == order["checkout_url"]
                    assert response.headers["Cache-Control"] == "no-store"
                    assert response.headers["Referrer-Policy"] == "no-referrer"
                async with session.get(base + path, allow_redirects=False) as response:
                    assert response.status == 302
                await asyncio.sleep(0)  # выполняется отдельная задача уведомления
                texts = app.bot.texts_to(8001)
                assert len(texts) == 1 and "Если Вы уже завершили оплату" in texts[0]
                assert "несколько минут" in texts[0]
                assert (await app.db.get_user(8001))["status"] == "new"
                assert not app.bot.invites
                await app.db.set_order_status(order["order_id"], "paid")
                async with session.get(base + path, allow_redirects=False) as response:
                    assert response.status == 404
        finally:
            await runner.cleanup()
            await app.db.close()
    asyncio.run(scenario())


def test_parallel_clicks_and_notification_failure_do_not_break_redirect():
    async def scenario():
        app = await make_app()
        runner = web.AppRunner(create_webhook_app(app), access_log=None)
        try:
            await app.db.upsert_user(8002)
            destination, _, order_id, _ = await app.payments.create_payment_link(8002)
            path = urlsplit(await app.payments.checkout_button_url(order_id, destination)).path
            app.bot.blocked_users.add(8002)
            await runner.setup()
            await web.TCPSite(runner, "127.0.0.1", 0).start()
            base = f"http://127.0.0.1:{runner.addresses[0][1]}"
            async with ClientSession() as session:
                async def click():
                    async with session.get(base + path, allow_redirects=False) as response:
                        return response.status, response.headers.get("Location")
                results = await asyncio.gather(*(click() for _ in range(5)))
            assert results == [(302, destination)] * 5
            await asyncio.sleep(0)
            assert (await app.db.get_order(order_id))["checkout_notified"] == 1
            assert (await app.db.get_user(8002))["status"] == "new"
        finally:
            await runner.cleanup()
            await app.db.close()
    asyncio.run(scenario())


def test_no_public_https_address_keeps_direct_payment_link():
    async def scenario():
        for base in (None, "http://localhost:8080", "https://localhost", "https://example.test/?x=y"):
            app = await make_app(public_base_url=base)
            try:
                await app.db.upsert_user(8003)
                url, _, order, _ = await app.payments.create_payment_link(8003)
                assert await app.payments.checkout_button_url(order, url) == url
            finally:
                await app.db.close()
    asyncio.run(scenario())


def test_old_database_migrates_and_redirect_survives_restart(tmp_path):
    path = str(tmp_path / "club.db")
    old_schema = SCHEMA.replace(
        "    checkout_url TEXT,                                -- точная ссылка на Prodamus\n", ""
    ).replace(
        "    checkout_token TEXT,                              -- случайный идентификатор перехода\n", ""
    ).replace("    checkout_notified INTEGER NOT NULL DEFAULT 0,\n", "")
    with sqlite3.connect(path) as conn:
        conn.executescript(old_schema)
        conn.execute("INSERT INTO orders(order_id, tg_id, amount, created_at) VALUES (?, ?, ?, ?)",
                     ("old-order", 8004, 790, "2026-01-01"))

    async def scenario():
        app = await make_app(db_path=path)
        try:
            old = await app.db.get_order("old-order")
            assert old["checkout_url"] is None and old["checkout_notified"] == 0
            await app.db.upsert_user(8004)
            url, _, order, _ = await app.payments.create_payment_link(8004)
            button = await app.payments.checkout_button_url(order, url)
        finally:
            await app.db.close()
        db = Database(path)
        await db.connect()
        try:
            await db.init()
            row = await db.get_order_by_checkout_token(urlsplit(button).path.rsplit("/", 1)[-1])
            assert row["checkout_url"] == url and row["order_id"] == order
        finally:
            await db.close()
    asyncio.run(scenario())
