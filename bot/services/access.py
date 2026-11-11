"""Выдача и отзыв доступа: одноразовые ссылки, исключение из канала и чата."""

from __future__ import annotations

import logging
import math
import time
from datetime import datetime, timedelta

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
)
from aiogram.types import InlineKeyboardMarkup

from .. import texts
from ..config import Settings
from ..db import Database
from ..keyboards import access_kb, member_kb
from ..utils import fmt_date, fmt_price, from_iso, human_username, utcnow

log = logging.getLogger(__name__)

_NOT_PARTICIPANT_MARKERS = (
    "user not found",
    "participant",
    "user is not a member",
    "not a member",
    "member not found",
    "user_id_invalid",
    "chat not found",
)


class AccessService:
    def __init__(self, bot: Bot, settings: Settings, db: Database) -> None:
        self.bot = bot
        self.settings = settings
        self.db = db

    # ------------------------------------------------------------------ ссылки

    async def create_invite_link(self, chat_id: int | str) -> str:
        """Одноразовая ссылка: 1 переход, живёт 24 часа."""
        expire_date = int(time.time()) + self.settings.invite_link_ttl_hours * 3600
        link = await self.bot.create_chat_invite_link(
            chat_id=chat_id,
            name=f"club-{int(time.time())}",
            member_limit=1,
            expire_date=expire_date,
        )
        return link.invite_link

    async def create_access_links(self) -> tuple[str, str]:
        channel_link = await self.create_invite_link(self.settings.channel_id)
        chat_link = await self.create_invite_link(self.settings.chat_id)
        return channel_link, chat_link

    async def access_keyboard(self, show_cancel: bool = False) -> InlineKeyboardMarkup:
        channel_link, chat_link = await self.create_access_links()
        return access_kb(channel_link, chat_link, show_cancel=show_cancel)

    # ------------------------------------------------------- сообщения участнику

    async def send_access(
        self,
        tg_id: int,
        *,
        manual: bool = False,
        paid_until: datetime | None = None,
        text: str | None = None,
    ) -> tuple[bool, str | None]:
        """Отправляет поздравление со ссылками на канал и чат."""
        try:
            keyboard = await self.access_keyboard()
        except (TelegramAPIError, TelegramBadRequest) as exc:
            log.exception("Не удалось создать ссылки-приглашения")
            await self.notify_admins(
                "⚠️ <b>Не получилось создать ссылки-приглашения.</b>\n\n"
                f"Проверьте, что бот — администратор канала и чата с правом "
                f"«Приглашать пользователей».\n\n<code>{exc}</code>"
            )
            await self._safe_send(tg_id, texts.LINKS_ERROR.format(support=self._support()))
            return False, str(exc)

        if text is None:
            if manual:
                until = paid_until or utcnow()
                text = texts.PAYMENT_SUCCESS_MANUAL.format(
                    days=self.settings.subscription_days, date=fmt_date(until, self.settings.timezone)
                )
            else:
                text = texts.PAYMENT_SUCCESS + texts.PAYMENT_SUCCESS_NOTE

        return await self._safe_send(tg_id, text, keyboard)

    async def send_renewed(self, tg_id: int, paid_until: datetime) -> tuple[bool, str | None]:
        text = texts.PAYMENT_RENEWED.format(
            date=fmt_date(paid_until, self.settings.timezone),
            price=fmt_price(self.settings.price, self.settings.currency),
        )
        return await self._safe_send(tg_id, text, member_kb(show_cancel=True))

    async def send_reminder(
        self,
        tg_id: int,
        paid_until: datetime,
        stage: str = "soon",
        pay_url: str | None = None,
        price_label: str | None = None,
    ) -> bool:
        """stage: soon — за N дней, today — в день окончания, late — на следующий день.

        pay_url — ссылка на оплату: тогда кнопка продления ведёт сразу на форму
        Prodamus (один клик вместо двух).
        """
        from ..keyboards import renew_kb, renew_url_kb

        price = price_label or fmt_price(self.settings.price, self.settings.currency)
        if stage == "today":
            text = texts.RENEWAL_TODAY.format(price=price)
        elif stage == "late":
            deadline = paid_until + timedelta(days=self.settings.grace_days)
            seconds = (deadline - utcnow()).total_seconds()
            days_left = max(1, math.ceil(seconds / 86400))
            text = texts.RENEWAL_LATE.format(
                price=price, days=days_left, days_word=_days_word(days_left)
            )
        else:
            text = texts.RENEWAL_REMINDER.format(price=price)
        keyboard = renew_url_kb(pay_url, price) if pay_url else renew_kb(price)
        ok, _ = await self._safe_send(tg_id, text, keyboard)
        return ok

    async def send_failure(self, tg_id: int) -> bool:
        from ..keyboards import retry_kb

        ok, _ = await self._safe_send(tg_id, texts.PAYMENT_FAILED, retry_kb())
        return ok

    async def send_farewell(self, tg_id: int) -> bool:
        from ..keyboards import return_kb

        text = texts.SUBSCRIPTION_ENDED.format(
            price=fmt_price(self.settings.price, self.settings.currency)
        )
        ok, _ = await self._safe_send(tg_id, text, return_kb())
        return ok

    async def send_links_again(
        self, tg_id: int, show_cancel: bool = False
    ) -> tuple[bool, str | None]:
        try:
            keyboard = await self.access_keyboard(show_cancel=show_cancel)
        except (TelegramAPIError, TelegramBadRequest) as exc:
            log.exception("Не удалось создать ссылки-приглашения")
            return False, str(exc)
        return await self._safe_send(tg_id, texts.LINKS_RESENT, keyboard)

    # ------------------------------------------------------------- исключение

    async def kick(self, tg_id: int, *, revoke_messages: bool = False) -> dict[str, bool]:
        """Исключает пользователя из канала и чата, оставляя возможность вернуться."""
        result = {}
        for name, chat in (("channel", self.settings.channel_id), ("chat", self.settings.chat_id)):
            result[name] = await self._remove_from(chat, tg_id, revoke_messages=revoke_messages)
        return result

    async def _remove_from(self, chat: int | str, tg_id: int, *, revoke_messages: bool) -> bool:
        removed = False
        try:
            await self.bot.ban_chat_member(
                chat_id=chat, user_id=tg_id, revoke_messages=revoke_messages
            )
            removed = True
        except TelegramBadRequest as exc:
            if _is_not_participant(str(exc)):
                log.info("Пользователь %s уже не состоит в %s", tg_id, chat)
            else:
                log.warning("ban_chat_member(%s, %s) не удался: %s", chat, tg_id, exc)
        except TelegramAPIError as exc:
            log.warning("ban_chat_member(%s, %s) не удался: %s", chat, tg_id, exc)
        try:
            # снимаем бан, чтобы человек мог вернуться по новой ссылке
            await self.bot.unban_chat_member(chat_id=chat, user_id=tg_id, only_if_banned=True)
        except TelegramAPIError as exc:
            log.debug("unban_chat_member(%s, %s): %s", chat, tg_id, exc)
        return removed

    # ------------------------------------------------------------- уведомления

    async def notify_admins(self, text: str, keyboard: InlineKeyboardMarkup | None = None) -> None:
        for admin_id in self.settings.admin_ids:
            await self._safe_send(admin_id, text, keyboard)

    async def notify_payment(
        self,
        tg_id: int,
        amount: int,
        *,
        renewal: bool = False,
        promo_code: str | None = None,
        username: str | None = None,
        currency: str | None = None,
    ) -> None:
        user = await self.db.get_user(tg_id)
        template = (
            texts.PAYMENT_ADMIN_NOTICE_RENEWAL if renewal else texts.PAYMENT_ADMIN_NOTICE
        )
        extra = ""
        if promo_code:
            extra += f"Промокод: <code>{promo_code}</code>\n"
        until = from_iso(user["paid_until"]) if user else None
        if until:
            extra += f"Доступ до: {fmt_date(until, self.settings.timezone)}"
        await self.notify_admins(
            template.format(
                username=username or human_username(user),
                tg_id=tg_id,
                amount=fmt_price(amount, currency or self.settings.currency),
                extra=extra,
            )
        )

    # ------------------------------------------------------------------ служебное

    async def _safe_send(
        self,
        tg_id: int,
        text: str,
        keyboard: InlineKeyboardMarkup | None = None,
    ) -> tuple[bool, str | None]:
        try:
            await self.bot.send_message(
                chat_id=tg_id, text=text, reply_markup=keyboard, disable_web_page_preview=True
            )
            return True, None
        except TelegramForbiddenError as exc:
            log.warning("Пользователь %s заблокировал бота: %s", tg_id, exc)
            return False, "бот заблокирован пользователем"
        except TelegramAPIError as exc:
            log.warning("send_message(%s) не удался: %s", tg_id, exc)
            return False, str(exc)

    def _support(self) -> str:
        return self.settings.support_contact or "администратора клуба"


def _days_word(days: int) -> str:
    """1 день / 2 дня / 5 дней."""
    if days % 10 == 1 and days % 100 != 11:
        return "день"
    if days % 10 in (2, 3, 4) and days % 100 not in (12, 13, 14):
        return "дня"
    return "дней"


def _is_not_participant(error: str) -> bool:
    lowered = error.lower()
    return any(marker in lowered for marker in _NOT_PARTICIPANT_MARKERS)
