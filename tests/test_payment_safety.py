"""Проверки безопасности сопоставления и идемпотентности Prodamus."""
import asyncio
from datetime import timedelta

from aiohttp import ClientSession, FormData, web

from bot.prodamus import sign_payload
from bot.utils import from_iso, utcnow
from bot.webhook import create_webhook_app
from .helpers import make_app


def test_numeric_internal_id_never_matches_local_order():
    async def run():
        app = await make_app()
        try:
            await app.db.upsert_user(901)
            await app.db.create_order(order_id="51283964", tg_id=901, amount=790, is_recurring=False)
            result = await app.payments.handle({"order_id": "51283964", "order_num": "",
                "payment_status": "success", "sum": "790", "customer_extra": "форма 901"})
            assert result == "unknown-user"
            assert (await app.db.get_user(901))["paid_until"] is None
            assert (await app.db.get_order("51283964"))["status"] == "pending"
        finally:
            await app.db.close()
    asyncio.run(run())


def test_exact_order_renewal_and_parallel_duplicate():
    async def run():
        app = await make_app()
        try:
            await app.db.upsert_user(902)
            old = (utcnow() + timedelta(days=27)).replace(microsecond=0)
            await app.db.activate(902, old)
            _, _, order, _ = await app.payments.create_payment_link(902)
            payload = {"order_id": "9988", "order_num": order, "tg_user_id": "902",
                       "payment_status": "success", "sum": "790.00"}
            results = await asyncio.gather(*(app.payments.handle(payload) for _ in range(5)))
            assert results.count("renewed") == 1
            assert results.count("duplicate") == 4
            assert from_iso((await app.db.get_user(902))["paid_until"]) == old + timedelta(days=30)
            assert (await app.db.stats())["payments_total"] == 1
        finally:
            await app.db.close()
    asyncio.run(run())


def test_tg_id_requires_one_order_and_conflicts_are_rejected():
    async def run():
        app = await make_app()
        try:
            await app.db.upsert_user(903)
            _, _, order, _ = await app.payments.create_payment_link(903)
            await app.payments.create_payment_link(903)
            payload = {"order_id": "10001", "tg_user_id": "903", "payment_status": "success", "sum": "790"}
            assert await app.payments.handle(payload) == "unknown-user"
            assert await app.payments.handle({**payload, "order_num": order, "tg_user_id": "904"}) == "unknown-user"
            assert (await app.db.get_user(903))["paid_until"] is None
        finally:
            await app.db.close()
    asyncio.run(run())


def test_contacts_must_be_verified_and_unambiguous():
    async def run():
        app = await make_app()
        try:
            await app.db.upsert_user(905)
            await app.db.save_contacts(905, "123456789", None)
            payload = {"order_id": "20001", "payment_status": "success", "sum": "790", "customer_phone": "123456789"}
            assert await app.payments.handle(payload) == "unknown-user"
            await app.db._write("UPDATE users SET phone_verified=1 WHERE tg_id=905")
            await app.db.upsert_user(906)
            await app.db.save_contacts(906, "123456789", None)
            assert await app.payments.handle(payload) == "unknown-user"
            await app.db.save_contacts(906, "987654321", None)
            assert await app.payments.handle(payload) == "paid"
            assert await app.payments.handle(payload) == "duplicate"
            assert (await app.db.stats())["revenue_total"] == 790
            assert (await app.db.stats())["payments_total"] == 1
        finally:
            await app.db.close()
    asyncio.run(run())


def test_currency_without_charged_sum_falls_back_to_rubles():
    async def run():
        app = await make_app()
        try:
            await app.db.upsert_user(907)
            _, _, order, _ = await app.payments.create_foreign_link(907)
            assert await app.payments.handle({"order_id": "20002", "order_num": order,
                "payment_status": "success", "sum": "790.00", "currency": "usd"}) == "paid"
            assert "790 ₽" in app.bot.last_to(111)["text"]
            assert "790 $" not in app.bot.last_to(111)["text"]
            assert (await app.db.stats())["revenue_total"] == 790
        finally:
            await app.db.close()
    asyncio.run(run())


def test_multipart_signed_notification():
    async def run():
        app = await make_app()
        runner = web.AppRunner(create_webhook_app(app))
        try:
            await app.db.upsert_user(908)
            _, _, order, _ = await app.payments.create_foreign_link(908)
            payload = {"order_id": "20003", "order_num": order, "payment_status": "success",
                       "sum": "790.00", "currency": "usd", "currency_sum": "11.68"}
            form = FormData(default_to_multipart=True)
            for k, v in payload.items():
                form.add_field(k, v)
            await runner.setup()
            site = web.TCPSite(runner, "127.0.0.1", 0)
            await site.start()
            port = runner.addresses[0][1]
            async with ClientSession() as session:
                async with session.post(f"http://127.0.0.1:{port}{app.settings.webhook_path}",
                    data=form, headers={"Sign": sign_payload(payload, app.settings.prodamus_secret)}) as response:
                    assert response.status == 200
            assert "11.68 $" in app.bot.last_to(111)["text"]
            assert (await app.db.stats())["revenue_total"] == 790
        finally:
            await runner.cleanup()
            await app.db.close()
    asyncio.run(run())


def test_incomplete_nested_signature_cannot_authenticate_outer_fields():
    app_settings = __import__("tests.helpers", fromlist=["make_settings"]).make_settings()
    from bot.prodamus import ProdamusClient
    import json
    client = ProdamusClient(app_settings)
    signed = {"order_id": "1", "payment_status": "success"}
    data = {"submit": json.dumps(signed), "order_num": "attacker-order", "payment_status": "success"}
    signature = sign_payload(signed, app_settings.prodamus_secret)
    assert not client.verify_request(data, {"Sign": signature})
    assert not client.verify_raw(b"", signature, data)


def test_future_paid_until_is_extended_even_if_status_is_stale():
    from datetime import datetime, timezone
    from unittest.mock import patch

    async def run():
        app = await make_app()
        try:
            await app.db.upsert_user(909)
            old = datetime(2026, 12, 14, tzinfo=timezone.utc)
            await app.db.activate(909, old)
            await app.db.set_status(909, "expired")
            _, _, order, _ = await app.payments.create_payment_link(909)
            with patch("bot.db.utcnow", return_value=datetime(2026, 11, 17, tzinfo=timezone.utc)):
                result = await app.payments.handle({"order_id": "30001", "order_num": order,
                    "payment_status": "success", "sum": "790"})
            assert result == "renewed"
            assert from_iso((await app.db.get_user(909))["paid_until"]) == old + timedelta(days=30)
        finally:
            await app.db.close()
    asyncio.run(run())


def test_malformed_paid_until_is_not_overwritten():
    async def run():
        app = await make_app()
        try:
            await app.db.upsert_user(910)
            await app.db._write("UPDATE users SET paid_until='invalid' WHERE tg_id=910")
            _, _, order, _ = await app.payments.create_payment_link(910)
            try:
                await app.payments.handle({"order_id": "30002", "order_num": order,
                    "payment_status": "success", "sum": "790"})
                assert False, "invalid paid_until must not be overwritten"
            except ValueError:
                pass
            assert (await app.db.get_user(910))["paid_until"] == "invalid"
            assert (await app.db.get_order(order))["status"] == "pending"
            assert (await app.db.stats())["payments_total"] == 0
        finally:
            await app.db.close()
    asyncio.run(run())
