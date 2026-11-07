"""Интеграционный тест HTTP-приёмника вебхуков: подпись, 200/400, выдача доступа."""

from __future__ import annotations

import asyncio

from aiohttp import ClientSession, web

from bot.prodamus import sign_payload
from bot.webhook import create_webhook_app

from .helpers import make_app


def test_webhook_endpoint() -> None:
    async def scenario() -> None:
        app = await make_app()
        await app.db.upsert_user(777, "webhook_user", "Тест")
        _url, _amount, order_id, _d = await app.payments.create_payment_link(777)

        aiohttp_app = create_webhook_app(app)  # type: ignore[arg-type]
        runner = web.AppRunner(aiohttp_app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = runner.addresses[0][1]
        base = f"http://127.0.0.1:{port}"

        payload = {
            "order_id": order_id,
            "payment_status": "success",
            "sum": "790",
            "customer_extra": "tg:777",
            "subscription": "4242",
        }
        signature = sign_payload(payload, app.settings.prodamus_secret)

        async with ClientSession() as session:
            async with session.get(f"{base}/health") as response:
                assert response.status == 200

            # неверная подпись -> 400, доступ не выдаётся
            async with session.post(
                base + app.settings.webhook_path,
                data=payload,
                headers={"Sign": "deadbeef"},
            ) as response:
                assert response.status == 400

            user = await app.db.get_user(777)
            assert user["status"] == "new"

            # верная подпись -> 200 success
            async with session.post(
                base + app.settings.webhook_path,
                data=payload,
                headers={"Sign": signature},
            ) as response:
                assert response.status == 200
                assert (await response.text()).strip() == "success"

            # обработка идёт в фоновой задаче — ждём результат
            for _ in range(50):
                user = await app.db.get_user(777)
                if user["status"] == "active":
                    break
                await asyncio.sleep(0.05)
            assert user["status"] == "active"

            async with session.get(base + app.settings.webhook_path) as response:
                assert response.status == 200
                assert "alive" in await response.text()

        await runner.cleanup()
        await app.db.close()

    asyncio.run(scenario())
