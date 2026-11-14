"""Администраторы: главные из ADMIN_IDS и добавленные прямо в боте."""

from __future__ import annotations

import asyncio

from bot.filters import IsAdmin
from bot.handlers import admin as admin_handlers

from .helpers import buttons_of, make_app
from .test_handlers import FakeCallback, FakeMessage, FakeState, _handler

OWNER_ID = 111
MARINA_ID = 2138023308


def test_admin_can_add_marina_from_panel_by_numeric_id() -> None:
    async def scenario() -> None:
        app = await make_app()
        await app.db.upsert_user(MARINA_ID, "marina_moii", "Марина")
        router = admin_handlers.build_router(app)  # type: ignore[arg-type]

        # 1. в панели есть кнопка «Администраторы»
        panel = FakeMessage("/admin", user_id=OWNER_ID)
        await _handler(router, "message", "cmd_admin")(panel, FakeState())
        assert "Администраторы" in buttons_of(panel.answers[-1]["keyboard"])

        # 2. открыли список и нажали «Добавить администратора»
        call = FakeCallback("adm:admins", FakeMessage(user_id=OWNER_ID), user_id=OWNER_ID)
        await _handler(router, "callback_query", "cb_admins")(call)
        assert "Марина" not in call.message.answers[-1]["text"]

        add = FakeCallback("adm:admin_add", FakeMessage(user_id=OWNER_ID), user_id=OWNER_ID)
        state = FakeState()
        await _handler(router, "callback_query", "cb_admin_add")(add, state)
        assert state.state is not None

        # 3. прислали числовой ID
        message = FakeMessage(str(MARINA_ID), user_id=OWNER_ID)
        await _handler(router, "message", "msg_admin_add")(message, state)
        assert "теперь администратор" in message.answers[-1]["text"]
        assert state.state is None

        # 4. Марина теперь админ: фильтр её пропускает, запись есть в БД
        assert app.settings.is_admin(MARINA_ID)
        assert await IsAdmin(app.settings)(FakeMessage(user_id=MARINA_ID))
        assert not await IsAdmin(app.settings)(FakeMessage(user_id=999))
        assert [int(a["tg_id"]) for a in await app.db.list_admins()] == [MARINA_ID]

        await app.db.close()

    asyncio.run(scenario())


def test_added_admin_gets_payment_warnings() -> None:
    """Уведомления админам уходят и главному, и добавленным в боте."""

    async def scenario() -> None:
        app = await make_app()
        await app.db.add_admin(MARINA_ID, "Марина", OWNER_ID)
        app.settings.extra_admin_ids.update(await app.db.extra_admin_ids())

        await app.access.notify_admins("тест")

        assert app.bot.texts_to(OWNER_ID) == ["тест"]
        assert app.bot.texts_to(MARINA_ID) == ["тест"]
        await app.db.close()

    asyncio.run(scenario())


def test_owner_cannot_be_removed_and_added_admin_can() -> None:
    async def scenario() -> None:
        app = await make_app()
        await app.db.add_admin(MARINA_ID, "Марина", OWNER_ID)
        app.settings.extra_admin_ids.add(MARINA_ID)
        router = admin_handlers.build_router(app)  # type: ignore[arg-type]

        # главного из ADMIN_IDS убрать нельзя
        owner = FakeCallback(
            f"adm:admin_del:{OWNER_ID}", FakeMessage(user_id=OWNER_ID), user_id=OWNER_ID
        )
        await _handler(router, "callback_query", "cb_admin_del")(owner)
        assert "главный администратор" in owner.answered[-1]
        assert app.settings.is_admin(OWNER_ID)

        # Марину — можно
        gone = FakeCallback(
            f"adm:admin_del:{MARINA_ID}", FakeMessage(user_id=OWNER_ID), user_id=OWNER_ID
        )
        await _handler(router, "callback_query", "cb_admin_del")(gone)
        assert not app.settings.is_admin(MARINA_ID)
        assert await app.db.list_admins() == []

        await app.db.close()

    asyncio.run(scenario())
