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


def test_stats_screen_renders_all_placeholders() -> None:
    """Статистика падала с KeyError: 'autorenew_off' — все числа должны подставляться."""

    async def scenario() -> None:
        from datetime import timedelta

        from bot.handlers import admin as admin_handlers
        from bot.utils import utcnow
        from tests.test_handlers import FakeMessage, _handler

        app = await make_app()
        await app.db.upsert_user(MARINA_ID, "marina_moii", "Марина")
        await app.db.activate(MARINA_ID, utcnow() + timedelta(days=30), source="manual")
        await app.db.set_auto_renew(MARINA_ID, False)

        router = admin_handlers.build_router(app)  # type: ignore[arg-type]
        message = FakeMessage("/stats", user_id=OWNER_ID)
        await _handler(router, "message", "cmd_stats")(message)

        text = message.answers[-1]["text"]
        assert "Статистика" in text
        # все подстановки из шаблона заполнены — ни одной «{...}»
        assert "{" not in text and "}" not in text
        # и число отключивших автопродление доехало до экрана
        assert "Отменили автопродление (доступ ещё действует): <b>1</b>" in text

        await app.db.close()

    asyncio.run(scenario())


def test_each_panel_button_asks_its_own_question() -> None:
    """«Забрать доступ» больше не спрашивает «Кому выдаём доступ?»."""

    async def scenario() -> None:
        from bot.handlers import admin as admin_handlers
        from tests.test_handlers import FakeCallback, FakeMessage, FakeState, _handler

        app = await make_app()
        router = admin_handlers.build_router(app)  # type: ignore[arg-type]

        async def ask(handler_name: str, data: str) -> str:
            message = FakeMessage(user_id=OWNER_ID)
            call = FakeCallback(data, message, user_id=OWNER_ID)
            await _handler(router, "callback_query", handler_name)(call, FakeState())
            return message.answers[-1]["text"]

        grant = await ask("cb_grant", "adm:grant")
        revoke = await ask("cb_revoke", "adm:revoke")
        find = await ask("cb_find", "adm:find")

        assert "Кому выдаём доступ" in grant
        assert "У кого забираем доступ" in revoke
        assert "Кого ищем" in find
        assert len({grant, revoke, find}) == 3
        # подсказка про ввод во всех трёх одинаковая
        assert all("@username" in text for text in (grant, revoke, find))

        await app.db.close()

    asyncio.run(scenario())


def test_revoke_asks_for_confirmation_before_kicking() -> None:
    """«Забрать доступ» сначала спрашивает подтверждение, и только потом исключает."""

    async def scenario() -> None:
        from datetime import timedelta

        from bot.handlers import admin as admin_handlers
        from bot.keyboards import revoke_confirm_kb
        from bot.utils import utcnow
        from tests.helpers import buttons_of
        from tests.test_handlers import FakeCallback, FakeMessage, FakeState, _handler

        app = await make_app()
        await app.db.upsert_user(MARINA_ID, "marina_moii", "Marina")
        await app.db.activate(MARINA_ID, utcnow() + timedelta(days=30), source="manual")
        router = admin_handlers.build_router(app)  # type: ignore[arg-type]

        # 1. прислали имя — бот НЕ исключает, а спрашивает подтверждение
        message = FakeMessage("marina_moii", user_id=OWNER_ID)
        await _handler(router, "message", "revoke_user")(message, FakeState())
        answer = message.answers[-1]
        assert "Забрать доступ?" in answer["text"]
        assert "2138023308" in answer["text"]
        assert "Да, забрать доступ" in buttons_of(answer["keyboard"])
        assert "Оставить как есть" in buttons_of(answer["keyboard"])
        assert app.bot.banned == []  # никого не тронули

        # 2. нажали «Да» — вот теперь исключаем
        call = FakeCallback(
            f"adm:revoke_yes:{MARINA_ID}", FakeMessage(user_id=OWNER_ID), user_id=OWNER_ID
        )
        await _handler(router, "callback_query", "cb_revoke_yes")(call)
        assert "исключён из канала и чата" in call.message.answers[-1]["text"]
        assert {chat for chat, _ in app.bot.banned} == {
            app.settings.channel_id,
            app.settings.chat_id,
        }
        user = await app.db.get_user(MARINA_ID)
        assert user["status"] == "expired"

        # 3. «Оставить как есть» ведёт просто в панель — проверим саму клавиатуру
        keyboard = revoke_confirm_kb(MARINA_ID)
        assert buttons_of(keyboard) == ["Да, забрать доступ", "Оставить как есть"]

        await app.db.close()

    asyncio.run(scenario())


def test_member_card_has_revoke_button_with_confirmation() -> None:
    async def scenario() -> None:
        from bot.handlers import admin as admin_handlers
        from tests.helpers import buttons_of
        from tests.test_handlers import FakeCallback, FakeMessage, FakeState, _handler

        app = await make_app()
        await app.db.upsert_user(MARINA_ID, "marina_moii", "Marina")
        router = admin_handlers.build_router(app)  # type: ignore[arg-type]

        find = FakeMessage("marina_moii", user_id=OWNER_ID)
        await _handler(router, "message", "find_user")(find, FakeState())
        assert "Забрать доступ" in buttons_of(find.answers[-1]["keyboard"])

        call = FakeCallback(
            f"adm:revoke_ask:{MARINA_ID}", FakeMessage(user_id=OWNER_ID), user_id=OWNER_ID
        )
        await _handler(router, "callback_query", "cb_revoke_ask")(call)
        answer = call.message.answers[-1]
        assert "Забрать доступ?" in answer["text"]
        assert "Да, забрать доступ" in buttons_of(answer["keyboard"])
        assert app.bot.banned == []  # пока не подтвердили — никого не трогаем

        await app.db.close()

    asyncio.run(scenario())
