"""Фоновые задачи: напоминание о продлении и авто-кик при неоплате."""

from __future__ import annotations

import logging
from datetime import timedelta

from ..config import Settings
from ..db import Database
from ..utils import fmt_price, from_iso, to_iso, utcnow
from .access import AccessService
from .payments import PaymentProcessor

log = logging.getLogger(__name__)


# стадии напоминаний: за N дней до конца, в день окончания, на следующий день
REMINDER_STAGES = ("soon", "today", "late")


class ScheduledTasks:
    """Один «тик» раз в 10 минут.

    Бесплатный режим (без робота автосписаний Prodamus) — продление вручную,
    поэтому напоминаний три: за 3 дня, в день окончания и на следующий день.
    Каждое приходит один раз за период оплаты.
    """

    def __init__(
        self,
        settings: Settings,
        db: Database,
        access: AccessService,
        payments: PaymentProcessor | None = None,
    ) -> None:
        self.settings = settings
        self.db = db
        self.access = access
        self.payments = payments

    async def tick(self) -> dict[str, int]:
        reminded = await self.send_renewal_reminders()
        expired = await self.expire_subscriptions()
        if reminded or expired:
            log.info("Плановая проверка: напоминаний %s, исключений %s", reminded, expired)
        return {"reminded": reminded, "expired": expired}

    # ------------------------------- напоминания: за 3 дня, в день X, на следующий

    def stage_for(self, paid_until, now) -> int:
        """Какая стадия напоминания уместна сейчас (0 — рано, дальше индекс стадии)."""
        triggers = (
            paid_until - timedelta(days=self.settings.reminder_days_before),
            paid_until,
            paid_until + timedelta(days=1),
        )
        stage = 0
        for index, moment in enumerate(triggers, start=1):
            if now >= moment:
                stage = index
        return stage

    async def send_renewal_reminders(self) -> int:
        horizon = to_iso(utcnow() + timedelta(days=self.settings.reminder_days_before))
        users = await self.db.users_for_reminder(horizon)
        sent = 0
        now = utcnow()
        for user in users:
            paid_until = from_iso(user.get("paid_until"))
            if paid_until is None:
                continue

            stage = self.stage_for(paid_until, now)
            if stage <= int(user.get("reminder_stage") or 0):
                continue

            grace = timedelta(
                days=self.settings.grace_days
                if user.get("auto_renew", 1) and not user.get("exact_access_end", 0)
                else 0
            )
            if now > paid_until + grace:
                # доступ уже пора закрывать — не шлём «продлите», этим займётся авто-кик
                await self.db.set_reminder_stage(user["tg_id"], stage, user["paid_until"])
                continue

            pay_url, price_label = await self._renewal_url(user["tg_id"])
            ok = await self.access.send_reminder(
                user["tg_id"],
                paid_until,
                REMINDER_STAGES[stage - 1],
                pay_url=pay_url,
                price_label=price_label,
            )
            # фиксируем факт попытки в любом случае, чтобы не спамить каждые 10 минут
            await self.db.set_reminder_stage(user["tg_id"], stage, user["paid_until"])
            if ok:
                sent += 1
        return sent

    async def _renewal_url(self, tg_id: int) -> tuple[str | None, str | None]:
        """Ссылка и цена для кнопки в напоминании (если сервис оплат доступен)."""
        if self.payments is None:
            return None, None
        try:
            url, amount, order_id, currency = await self.payments.create_link_auto(tg_id)
            url = await self.payments.checkout_button_url(order_id, url)
            return url, fmt_price(amount, currency)
        except Exception:  # noqa: BLE001 — напоминание важнее кнопки
            log.exception("Не удалось создать ссылку на оплату для напоминания")
            return None, None

    # --------------------------------------- 30 дней + 1 день отсрочки

    async def expire_subscriptions(self) -> int:
        now = utcnow()
        users = await self.db.users_to_expire(to_iso(now))
        kicked = 0
        for user in users:
            paid_until = from_iso(user.get("paid_until"))
            if paid_until is None:
                continue
            # Явная дата от администратора действует без отсрочки. Обычная
            # оплаченная подписка сохраняет отсрочку при ожидаемом автоплатеже.
            auto_renew = bool(user.get("auto_renew", 1))
            exact = bool(user.get("exact_access_end", 0))
            deadline = paid_until + timedelta(
                days=self.settings.grace_days if auto_renew and not exact else 0
            )
            if deadline > now:
                continue
            tg_id = int(user["tg_id"])
            # сначала фиксируем статус: если что-то упадёт, повтора не будет
            await self.db.set_status(tg_id, "expired")
            result = await self.access.kick(tg_id)
            await self.access.send_farewell(tg_id)
            log.info("Доступ закрыт: tg_id=%s, канал=%s, чат=%s", tg_id, *result.values())
            kicked += 1
        return kicked


__all__ = ["ScheduledTasks"]
