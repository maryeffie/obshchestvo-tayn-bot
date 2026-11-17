"""Администратор задаёт точную дату, без повторной оплаты и продления на месяц."""
import asyncio
from datetime import datetime, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

from bot.filters import IsAdmin
from bot.handlers import admin as admin_handlers
from bot.utils import from_iso, to_iso
from .helpers import make_app
from .test_handlers import FakeCallback, FakeMessage, FakeState, _handler

FIXED_NOW = datetime(2026, 11, 17, 12, tzinfo=timezone.utc)
EXPECTED = datetime(2026, 12, 14, 23, 59, 59, tzinfo=ZoneInfo("Europe/Moscow"))


async def begin(router, state, tg_id):
    await _handler(router, "callback_query", "cb_date_start")(
        FakeCallback("adm:date", FakeMessage(user_id=111), user_id=111), state
    )
    await _handler(router, "message", "date_user")(FakeMessage(str(tg_id), user_id=111), state)


async def confirm(router, state):
    call = FakeCallback("adm:date_confirm", FakeMessage(user_id=111), user_id=111)
    await _handler(router, "callback_query", "cb_date_confirm")(call, state)
    return call


def test_corrects_existing_date_exactly_preserving_billing_and_history():
    async def scenario():
        app = await make_app()
        try:
            await app.db.upsert_user(9001, "member", "Имя")
            old_until = datetime(2027, 1, 17, 12, tzinfo=timezone.utc)
            await app.db.activate(9001, old_until, source="prodamus")
            await app.db.set_auto_renew(9001, False, cancelled=True)
            before = await app.db.get_user(9001)
            router = admin_handlers.build_router(app)
            state = FakeState()
            await begin(router, state, 9001)
            entry = FakeMessage("14.12.2026", user_id=111)
            await _handler(router, "message", "date_value")(entry, state)
            preview = entry.last_text
            assert "17.01.2027" in preview and "14.12.2026" in preview
            assert "23:59" in preview and "Europe/Moscow" in preview
            assert "короче текущего" in preview
            assert "автосписания не изменятся" in preview
            assert (await app.db.get_user(9001))["paid_until"] == before["paid_until"]
            call = await confirm(router, state)
            after = await app.db.get_user(9001)
            assert from_iso(after["paid_until"]) == EXPECTED
            assert after["status"] == "active"
            for key in ("auto_renew", "cancelled_at", "last_payment_at", "access_source", "last_currency"):
                assert after[key] == before[key]
            assert "14.12.2026" in call.message.last_text
            assert not app.bot.invites and not app.bot.texts_to(9001)
            assert (await app.db.stats())["payments_total"] == 0
            audits = await app.db._fetchall("SELECT * FROM admin_access_changes")
            assert len(audits) == 1 and audits[0]["admin_tg_id"] == 111
            assert audits[0]["tg_id"] == 9001
            assert audits[0]["old_paid_until"] == before["paid_until"]
            assert audits[0]["new_paid_until"] == to_iso(EXPECTED)
            await confirm(router, state)
            assert (await app.db.get_user(9001))["paid_until"] == after["paid_until"]
            assert len(await app.db._fetchall("SELECT * FROM admin_access_changes")) == 1
        finally:
            await app.db.close()
    with patch("bot.handlers.admin.utcnow", return_value=FIXED_NOW), patch("bot.utils.utcnow", return_value=FIXED_NOW):
        asyncio.run(scenario())


def test_new_user_receives_access_until_selected_date_without_recording_payment():
    async def scenario():
        app = await make_app()
        try:
            router = admin_handlers.build_router(app)
            state = FakeState()
            await begin(router, state, 9002)
            await _handler(router, "message", "date_value")(FakeMessage("14.12.2026", user_id=111), state)
            assert await app.db.get_user(9002) is None
            call = await confirm(router, state)
            user = await app.db.get_user(9002)
            assert from_iso(user["paid_until"]) == EXPECTED
            assert user["status"] == "active" and user["auto_renew"] == 0
            assert user["last_payment_at"] is None and user["access_source"] == "manual"
            assert len(app.bot.invites) == 2
            assert any("14.12.2026" in text and "включительно" in text for text in app.bot.texts_to(9002))
            assert "Ссылки отправлены" in call.message.last_text
            assert (await app.db.stats())["payments_total"] == 0
        finally:
            await app.db.close()
    with patch("bot.handlers.admin.utcnow", return_value=FIXED_NOW), patch("bot.utils.utcnow", return_value=FIXED_NOW):
        asyncio.run(scenario())


def test_invalid_date_and_cancel_do_not_change_access():
    async def scenario():
        app = await make_app()
        try:
            await app.db.upsert_user(9003)
            router = admin_handlers.build_router(app)
            state = FakeState()
            await begin(router, state, 9003)
            for value in ("14.12", "2026-12-14", "30.02.2027", "14.12.2025", "0"):
                msg = FakeMessage(value, user_id=111)
                await _handler(router, "message", "date_value")(msg, state)
                assert "Не удалось принять дату" in msg.last_text
                assert (await app.db.get_user(9003))["paid_until"] is None
            assert not await app.db._fetchall("SELECT * FROM admin_access_changes")
        finally:
            await app.db.close()
    with patch("bot.handlers.admin.utcnow", return_value=FIXED_NOW):
        asyncio.run(scenario())


def test_concurrent_payment_is_not_overwritten_by_pending_confirmation():
    async def scenario():
        app = await make_app()
        try:
            await app.db.upsert_user(9004)
            await app.db.activate(9004, datetime(2026, 12, 1, tzinfo=timezone.utc))
            router = admin_handlers.build_router(app)
            state = FakeState()
            await begin(router, state, 9004)
            await _handler(router, "message", "date_value")(FakeMessage("14.12.2026", user_id=111), state)
            _, _, order_id, _ = await app.payments.create_payment_link(9004)
            assert await app.payments.handle({"order_id": "90040", "order_num": order_id,
                "payment_status": "success", "sum": "790"}) in {"paid", "renewed"}
            paid = await app.db.get_user(9004)
            call = await confirm(router, state)
            assert "Ничего не изменено" in call.message.last_text
            assert (await app.db.get_user(9004))["paid_until"] == paid["paid_until"]
            assert not await app.db._fetchall("SELECT * FROM admin_access_changes")
        finally:
            await app.db.close()
    with patch("bot.handlers.admin.utcnow", return_value=FIXED_NOW), patch("bot.utils.utcnow", return_value=FIXED_NOW), patch("bot.db.utcnow", return_value=FIXED_NOW):
        asyncio.run(scenario())


def test_date_handlers_are_admin_only():
    async def scenario():
        app = await make_app()
        try:
            router = admin_handlers.build_router(app)
            for observer, name in ((router.callback_query, "cb_date_start"),
                                   (router.callback_query, "cb_date_from_card"),
                                   (router.callback_query, "cb_date_confirm"),
                                   (router.message, "date_user"),
                                   (router.message, "date_value")):
                handler = next(h for h in observer.handlers if h.callback.__name__ == name)
                admin_filter = next(f.callback for f in handler.filters if isinstance(f.callback, IsAdmin))
                assert await admin_filter(FakeMessage(user_id=111))
                assert not await admin_filter(FakeMessage(user_id=999))
            confirmation = next(h for h in router.callback_query.handlers if h.callback.__name__ == "cb_date_confirm")
            admin_call = FakeCallback("adm:date_confirm", FakeMessage(user_id=111), user_id=111)
            stranger_call = FakeCallback("adm:date_confirm", FakeMessage(user_id=999), user_id=999)
            assert (await confirmation.check(admin_call, raw_state=admin_handlers.AdminStates.date_confirm.state))[0]
            assert not (await confirmation.check(admin_call, raw_state=None))[0]
            assert not (await confirmation.check(stranger_call, raw_state=admin_handlers.AdminStates.date_confirm.state))[0]
        finally:
            await app.db.close()
    asyncio.run(scenario())


def test_card_shortcut_and_blocked_user_still_saves_exact_date():
    async def scenario():
        app = await make_app()
        try:
            await app.db.upsert_user(9005)
            await app.db.set_status(9005, "expired")
            app.bot.blocked_users.add(9005)
            router = admin_handlers.build_router(app)
            state = FakeState()
            card = FakeCallback("adm:date:9005", FakeMessage(user_id=111), user_id=111)
            await _handler(router, "callback_query", "cb_date_from_card")(card, state)
            assert state.data["date_tg_id"] == 9005
            await _handler(router, "message", "date_value")(FakeMessage("14.12.2026", user_id=111), state)
            response = await confirm(router, state)
            assert from_iso((await app.db.get_user(9005))["paid_until"]) == EXPECTED
            assert "Дата сохранена" in response.message.last_text
            assert "не удалось" in response.message.last_text
            assert (await app.db.stats())["payments_total"] == 0
        finally:
            await app.db.close()
    with patch("bot.handlers.admin.utcnow", return_value=FIXED_NOW), patch("bot.utils.utcnow", return_value=FIXED_NOW):
        asyncio.run(scenario())


def test_invalid_and_oversized_telegram_id_rejected():
    async def scenario():
        app = await make_app()
        try:
            router = admin_handlers.build_router(app)
            state = FakeState()
            for value in ("0", "9" * 30):
                msg = FakeMessage(value, user_id=111)
                await _handler(router, "message", "date_user")(msg, state)
                assert "Не нашёл" in msg.last_text
            call = FakeCallback("adm:date:" + "9" * 30, FakeMessage(user_id=111), user_id=111)
            await _handler(router, "callback_query", "cb_date_from_card")(call, state)
            assert call.answered and not call.message.answers
            assert not await app.db._fetchall("SELECT * FROM admin_access_changes")
        finally:
            await app.db.close()
    asyncio.run(scenario())


def test_exact_date_has_no_grace_and_new_payment_restores_normal_grace():
    from datetime import timedelta
    async def scenario():
        app = await make_app(grace_days=2)
        try:
            router = admin_handlers.build_router(app)
            for tg_id in (9006, 9007):
                await app.db.upsert_user(tg_id)
                await app.db.activate(tg_id, datetime(2027, 1, 17, tzinfo=timezone.utc))
                state = FakeState()
                await begin(router, state, tg_id)
                await _handler(router, "message", "date_value")(FakeMessage("14.12.2026", user_id=111), state)
                await confirm(router, state)
                assert (await app.db.get_user(tg_id))["exact_access_end"] == 1
            _, _, order_id, _ = await app.payments.create_payment_link(9007)
            assert await app.payments.handle({"order_id": "90070", "order_num": order_id,
                "payment_status": "success", "sum": "790"}) == "renewed"
            renewed = await app.db.get_user(9007)
            assert renewed["exact_access_end"] == 0
            # Новая оплата продлила от установленного срока, а не от дня оплаты.
            assert from_iso(renewed["paid_until"]) == EXPECTED + timedelta(days=30)
            with patch("bot.services.tasks.utcnow", return_value=EXPECTED + timedelta(seconds=1)):
                assert await app.tasks.expire_subscriptions() == 1
            assert (await app.db.get_user(9006))["status"] == "expired"
            assert (await app.db.get_user(9007))["status"] == "active"
            with patch("bot.services.tasks.utcnow", return_value=from_iso(renewed["paid_until"]) + timedelta(seconds=1)):
                assert await app.tasks.expire_subscriptions() == 0
            assert (await app.db.get_user(9007))["status"] == "active"
        finally:
            await app.db.close()
    with patch("bot.handlers.admin.utcnow", return_value=FIXED_NOW), patch("bot.utils.utcnow", return_value=FIXED_NOW), patch("bot.db.utcnow", return_value=FIXED_NOW):
        asyncio.run(scenario())


def test_exact_date_column_migrates_for_old_users(tmp_path):
    import sqlite3
    from bot.db import Database, SCHEMA
    path = str(tmp_path / "old-club.db")
    old_schema = SCHEMA.replace(
        "    exact_access_end  INTEGER NOT NULL DEFAULT 0,    -- ручная дата без отсрочки автосписания\n", ""
    )
    with sqlite3.connect(path) as conn:
        conn.executescript(old_schema)
        conn.execute("INSERT INTO users(tg_id, status, paid_until, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                     (9008, "active", "2027-01-17T00:00:00+00:00", "2026-01-01", "2026-01-01"))
    async def scenario():
        db = Database(path)
        await db.connect()
        try:
            await db.init()
            user = await db.get_user(9008)
            assert user["status"] == "active" and user["paid_until"] == "2027-01-17T00:00:00+00:00"
            assert user["exact_access_end"] == 0
        finally:
            await db.close()
    asyncio.run(scenario())
