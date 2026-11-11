"""Тестовые двойники: поддельный Bot и сборка сервисов без сети."""

from __future__ import annotations

import itertools
from types import SimpleNamespace
from typing import Any

from bot.config import Settings
from bot.db import Database
from bot.prodamus import ProdamusClient, SubscriptionResult
from bot.services.access import AccessService
from bot.services.payments import PaymentProcessor
from bot.services.subscriptions import SubscriptionService
from bot.services.tasks import ScheduledTasks

_counter = itertools.count(1)


class ButtonTexts(list):
    """Подписи кнопок. `in` ищет подстроку, `==` игнорирует эмодзи-префиксы."""

    def __contains__(self, item) -> bool:  # type: ignore[override]
        return any(str(item) in text for text in self)

    def __eq__(self, other) -> bool:  # type: ignore[override]
        if isinstance(other, (list, tuple)) and len(other) == len(self):
            return all(str(expected) in text for expected, text in zip(other, self))
        return list.__eq__(self, other)

    def __ne__(self, other) -> bool:  # type: ignore[override]
        return not self.__eq__(other)


def buttons_of(keyboard) -> ButtonTexts:
    if keyboard is None:
        return ButtonTexts()
    return ButtonTexts(b.text for row in keyboard.inline_keyboard for b in row)


class FakeBot:
    """Повторяет только тот кусок Bot API, который использует бот."""

    def __init__(self) -> None:
        self.id = 999000111
        self.sent: list[dict[str, Any]] = []
        self.invites: list[dict[str, Any]] = []
        self.banned: list[tuple[Any, int]] = []
        self.unbanned: list[tuple[Any, int]] = []
        self.blocked_users: set[int] = set()
        self.session = SimpleNamespace(close=self._noop)

    async def _noop(self) -> None:
        return None

    async def send_message(self, chat_id: int, text: str, reply_markup=None, **kwargs):
        if chat_id in self.blocked_users:
            from aiogram.exceptions import TelegramForbiddenError

            raise TelegramForbiddenError(method=SimpleNamespace(__str__=lambda self: "sendMessage"), message="Forbidden: bot was blocked by the user")
        self.sent.append({"chat_id": chat_id, "text": text, "keyboard": reply_markup, **kwargs})
        return SimpleNamespace(message_id=next(_counter))

    async def create_chat_invite_link(self, chat_id, name=None, member_limit=None, expire_date=None):
        self.invites.append(
            {
                "chat_id": chat_id,
                "name": name,
                "member_limit": member_limit,
                "expire_date": expire_date,
            }
        )
        return SimpleNamespace(invite_link=f"https://t.me/+invite{next(_counter)}")

    async def ban_chat_member(self, chat_id, user_id, revoke_messages=False, **kwargs):
        self.banned.append((chat_id, user_id))
        return True

    async def unban_chat_member(self, chat_id, user_id, only_if_banned=False, **kwargs):
        self.unbanned.append((chat_id, user_id))
        return True

    async def get_chat_member(self, chat_id, user_id):
        return SimpleNamespace(status="administrator", can_invite_users=True)

    # ------------------------------------------------------------------ helpers

    def texts_to(self, chat_id: int) -> list[str]:
        return [item["text"] for item in self.sent if item["chat_id"] == chat_id]

    def last_to(self, chat_id: int) -> dict | None:
        for item in reversed(self.sent):
            if item["chat_id"] == chat_id:
                return item
        return None

    def clear(self) -> None:
        self.sent.clear()
        self.invites.clear()
        self.banned.clear()
        self.unbanned.clear()


def make_settings(**overrides) -> Settings:
    base: dict[str, Any] = dict(
        bot_token="123:TEST",
        admin_ids=(111,),
        channel_id=-1001111111111,
        chat_id=-1002222222222,
        price=790,
        currency="rub",
        price_eur=9,
        price_usd=10,
        price_byn=29,
        product_name="Подписка «Общество тайн» (1 месяц)",
        subscription_days=30,
        grace_days=1,
        reminder_days_before=3,
        invite_link_ttl_hours=24,
        prodamus_form_url="https://demo.payform.ru/",
        prodamus_secret="secret-key-for-tests",
        prodamus_subscription_id=4242,
        prodamus_sys=None,
        prodamus_sign_links=True,
        prodamus_send_tg_user_id=True,
        prodamus_url_success=None,
        prodamus_url_return=None,
        prodamus_foreign_url="https://payform.ru/nqd02rT/",
        webhook_host="0.0.0.0",
        webhook_port=8080,
        webhook_path="/prodamus/webhook",
        public_base_url="https://bot.example.com",
        db_path=":memory:",
        timezone=__import__("zoneinfo").ZoneInfo("Europe/Moscow"),
        support_contact="@marina",
        log_level="CRITICAL",
    )
    base.update(overrides)
    return Settings(**base)


class FakeApp:
    """Сервисы с подменённым сетевым вызовом Prodamus Rest API."""

    def __init__(self, settings: Settings, db: Database, bot: FakeBot) -> None:
        self.settings = settings
        self.db = db
        self.bot = bot
        self.prodamus = ProdamusClient(settings)
        self.access = AccessService(bot, settings, db)  # type: ignore[arg-type]
        self.payments = PaymentProcessor(settings, db, self.access, self.prodamus)
        self.subscriptions = SubscriptionService(
            settings, db, self.prodamus, self.access
        )
        self.tasks = ScheduledTasks(settings, db, self.access, self.payments)

        # по умолчанию Prodamus «отвечает успешно», в тестах можно выключить
        self.remote_ok = True
        self.remote_calls: list[dict] = []
        self.prodamus.set_subscription_activity = self._fake_set_activity  # type: ignore[method-assign]

    async def _fake_set_activity(self, **kwargs) -> SubscriptionResult:
        self.remote_calls.append(kwargs)
        return SubscriptionResult(
            ok=self.remote_ok,
            message="success" if self.remote_ok else "HTTP 500: internal error",
            tried="tg_user_id",
            remote=self.remote_ok,
        )


async def make_app(**overrides) -> FakeApp:
    settings = make_settings(**overrides)
    db = Database(settings.db_path)
    await db.connect()
    await db.init()
    return FakeApp(settings, db, FakeBot())
