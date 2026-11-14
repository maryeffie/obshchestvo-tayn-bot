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
