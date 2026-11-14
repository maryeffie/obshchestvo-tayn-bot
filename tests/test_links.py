"""Ссылки-приглашения: срок жизни и число входов.

История: Марина получила «This invite link has expired» на втором нажатии —
ссылка была одноразовой (member_limit=1). Теперь входов несколько.
"""

from __future__ import annotations

import asyncio
import time

from bot.services.access import AccessService

from .helpers import FakeBot, make_app, make_settings


def test_invite_link_opens_several_times_and_lives_24_hours() -> None:
    async def scenario() -> None:
        app = await make_app()
        await app.access.create_access_links()
        assert len(app.bot.invites) == 2
        for invite in app.bot.invites:
            assert invite["member_limit"] == 3
            assert invite["expire_date"] > time.time() + 23 * 3600
            assert invite["expire_date"] < time.time() + 25 * 3600
        await app.db.close()

    asyncio.run(scenario())


def test_zero_means_unlimited_entries_within_24_hours() -> None:
    async def scenario() -> None:
        settings = make_settings(invite_link_uses=0)
        bot = FakeBot()
        service = AccessService(bot, settings, None)  # type: ignore[arg-type]
        await service.create_invite_link(settings.channel_id)
        assert bot.invites[0]["member_limit"] is None
        assert bot.invites[0]["expire_date"] > time.time() + 23 * 3600

    asyncio.run(scenario())


def test_banned_user_is_unbanned_before_new_links() -> None:
    """Если человека когда-то выгнали, остаточный бан не пустит по ссылке."""

    async def scenario() -> None:
        app = await make_app()
        app.bot.member_status = "kicked"

        ok, error = await app.access.send_links_again(555)

        assert ok and error is None
        chats = {chat for chat, _ in app.bot.unbanned}
        assert chats == {app.settings.channel_id, app.settings.chat_id}
        assert len(app.bot.invites) == 2
        await app.db.close()

    asyncio.run(scenario())


def test_check_command_shows_statuses_and_sends_links() -> None:
    from bot.handlers import admin as admin_handlers
    from tests.test_handlers import FakeMessage, _handler

    async def scenario() -> None:
        app = await make_app()
        app.bot.member_status = "kicked"
        router = admin_handlers.build_router(app)  # type: ignore[arg-type]

        message = FakeMessage("/check 2138023308", user_id=111)
        await _handler(router, "message", "cmd_check")(message)

        text = message.answers[-1]["text"]
        assert "2138023308" in text
        assert "разбанен" in text
        assert "Свежие ссылки" in text and "отправлены" in text
        assert any("Новые ссылки" in t for t in app.bot.texts_to(2138023308))
        await app.db.close()

    asyncio.run(scenario())


def test_check_command_without_id_prints_hint() -> None:
    from bot.handlers import admin as admin_handlers
    from tests.test_handlers import FakeMessage, _handler

    async def scenario() -> None:
        app = await make_app()
        router = admin_handlers.build_router(app)  # type: ignore[arg-type]
        message = FakeMessage("/check", user_id=111)
        await _handler(router, "message", "cmd_check")(message)
        assert "Формат" in message.answers[-1]["text"]
        await app.db.close()

    asyncio.run(scenario())


def test_member_card_has_check_button_and_it_works() -> None:
    """В карточке участника есть кнопка «Проверить вход в канал»."""

    async def scenario() -> None:
        from bot.handlers import admin as admin_handlers
        from tests.helpers import buttons_of
        from tests.test_handlers import FakeCallback, FakeMessage, FakeState, _handler

        app = await make_app()
        app.bot.member_status = "kicked"
        await app.db.upsert_user(2138023308, "marina_moii", "Marina")
        router = admin_handlers.build_router(app)  # type: ignore[arg-type]

        find = FakeMessage("marina_moii", user_id=111)
        await _handler(router, "message", "find_user")(find, FakeState())
        card = find.answers[-1]
        assert "2138023308" in card["text"]
        assert "Проверить вход в канал" in buttons_of(card["keyboard"])
        assert "Выдать доступ на 30 дней" in buttons_of(card["keyboard"])

        call = FakeCallback(
            "adm:check:2138023308", FakeMessage(user_id=111), user_id=111
        )
        await _handler(router, "callback_query", "cb_check")(call)
        text = call.message.answers[-1]["text"]
        assert "разбанен" in text and "отправлены" in text
        assert "Бан снят" in text

        await app.db.close()

    asyncio.run(scenario())


def test_check_explains_when_user_is_already_inside() -> None:
    """«Ссылка недействительна» часто значит «ты уже в канале»."""

    async def scenario() -> None:
        from bot.handlers import admin as admin_handlers
        from tests.test_handlers import FakeMessage, _handler

        app = await make_app()
        app.bot.member_status = "creator"  # Марина — владелец канала
        router = admin_handlers.build_router(app)  # type: ignore[arg-type]

        message = FakeMessage("/check 2138023308", user_id=111)
        await _handler(router, "message", "cmd_check")(message)

        text = message.answers[-1]["text"]
        assert "уже внутри (владелец канала)" in text
        assert "вступать не нужно" in text
        await app.db.close()

    asyncio.run(scenario())
