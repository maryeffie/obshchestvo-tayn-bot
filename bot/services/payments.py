"""Обработка платежей: вебхуки Prodamus, выдача и продление доступа."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta

from ..config import Settings
from ..db import Database
from ..prodamus import Notification, ProdamusClient
from ..utils import fmt_price, from_iso, human_username, utcnow
from .access import AccessService

log = logging.getLogger(__name__)


class PaymentProcessor:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        access: AccessService,
        prodamus: ProdamusClient,
    ) -> None:
        self.settings = settings
        self.db = db
        self.access = access
        self.prodamus = prodamus

    # ------------------------------------------------------------- создание заказа

    @staticmethod
    def new_order_id(tg_id: int) -> str:
        """order_id в Prodamus: содержит tg_id (страховка) + уникальный хвост."""
        return f"tg{tg_id}-{uuid.uuid4().hex[:12]}"

    async def create_payment_link(
        self, tg_id: int, *, promo_code: str | None = None
    ) -> tuple[str, int, str, int]:
        """Возвращает (ссылка, итоговая сумма, order_id, размер скидки)."""
        discount = 0
        if promo_code:
            ok, reason = await self.db.promo_is_valid(promo_code)
            if ok:
                promo = await self.db.get_promo(promo_code)
                discount = round(self.settings.price * promo["discount_percent"] / 100)
                promo_code = promo["code"]
            else:
                log.info("Промокод %s отклонён: %s", promo_code, reason)
                promo_code, discount = None, 0

        amount = self.settings.price - discount
        order_id = self.new_order_id(tg_id)
        await self.db.create_order(
            order_id=order_id,
            tg_id=tg_id,
            amount=amount,
            promo_code=promo_code,
            is_recurring=self.settings.is_recurrent,
        )
        url = self.prodamus.payment_url(tg_id, order_id, self.settings.price, discount)
        return url, amount, order_id, discount

    async def create_foreign_link(self, tg_id: int, currency: str) -> tuple[str, int, str]:
        """Разовая оплата в валюте — для покупателей из других стран.

        Подписка (автосписание) в Prodamus возможна только в рублях и только
        по картам РФ, поэтому иностранцам выдаём разовый счёт на месяц.
        """
        currency = currency.lower()
        amount = self.settings.foreign_prices.get(currency)
        if not amount:
            raise ValueError(f"Валюта {currency} не настроена")
        order_id = self.new_order_id(tg_id)
        await self.db.create_order(
            order_id=order_id,
            tg_id=tg_id,
            amount=amount,
            is_recurring=False,
            currency=currency,
        )
        url = self.prodamus.payment_url(tg_id, order_id, amount, currency=currency)
        return url, amount, order_id

    async def create_link_auto(self, tg_id: int) -> tuple[str, int, str, str]:
        """Ссылка для «Продлить»: в валюте прошлой оплаты, если она была не в рублях."""
        user = await self.db.get_user(tg_id) or {}
        currency = str(user.get("last_currency") or "rub").lower()
        if currency != "rub" and currency in self.settings.foreign_prices:
            url, amount, order_id = await self.create_foreign_link(tg_id, currency)
            return url, amount, order_id, currency
        url, amount, order_id, _discount = await self.create_payment_link(tg_id)
        return url, amount, order_id, "rub"

    # ------------------------------------------------------------ обработка вебхука

    async def handle(self, data: dict) -> str:
        """Обрабатывает уведомление Prodamus. Возвращает метку результата для логов."""
        note = self.prodamus.parse(data)
        order = await self.db.get_order(note.order_id) if note.order_id else None
        order_currency = str((order or {}).get("currency") or "rub").lower()
        # в статистике всё считаем в рублях: один оплаченный месяц = цена подписки
        stats_amount = note.amount if order_currency == "rub" else self.settings.price
        await self.db.add_payment(
            order_id=note.order_id,
            tg_id=note.tg_id,
            amount=stats_amount,
            kind=note.kind,
            is_renewal=note.is_renewal,
            status_text=note.status_text,
            raw=data,
        )

        tg_id = await self._resolve_user(note)
        if tg_id is None:
            log.warning(
                "Не удалось определить пользователя из уведомления (order_id=%s, extra=%s)",
                note.order_id,
                data.get("customer_extra"),
            )
            await self.access.notify_admins(
                "⚠️ <b>Платёж пришёл, но пользователя не удалось определить.</b>\n\n"
                f"order_id: <code>{note.order_id}</code>\n"
                f"Сумма: {note.amount}\n"
                f"Телефон: {note.phone}\n\n"
                "Выдайте доступ вручную: /admin → «Выдать доступ»."
            )
            return "unknown-user"

        user = await self.db.get_user(tg_id)
        if user is None:
            user = await self.db.upsert_user(tg_id)

        if note.kind == "failed":
            await self.db.set_order_status(note.order_id, "failed") if order else None
            await self.access.send_failure(tg_id)
            log.info("Неуспешная оплата у %s: %s", tg_id, note.status_text)
            return "failed"

        if note.kind == "cancelled":
            await self.db.set_auto_renew(tg_id, False, cancelled=True)
            log.info("Подписка %s завершена/отменена: %s", tg_id, note.status_text)
            await self.access.notify_admins(
                "🔁 <b>Подписка отменена на стороне Prodamus</b>\n\n"
                f"Пользователь: {human_username(user)}\n"
                f"ID: <code>{tg_id}</code>\n"
                f"Причина: {note.status_text or 'отписка / завершение подписки'}\n\n"
                "Автосписание в базе бота выключено, доступ закроется в конце периода."
            )
            return "subscription-cancelled"

        if note.kind != "paid":
            log.info("Информационное уведомление Prodamus: %s", note.status_text)
            return "info"

        # --- успешная оплата -------------------------------------------------
        if order is not None and order["status"] == "paid":
            log.info("Повторный вебхук по заказу %s — пропускаем", note.order_id)
            return "duplicate"

        if note.phone or note.email:
            await self.db.save_contacts(tg_id, note.phone, note.email)

        now = utcnow()
        current_until = from_iso(user.get("paid_until"))
        is_active = user.get("status") == "active" and current_until and current_until > now
        is_renewal = bool(is_active)

        if is_active:
            base = max(current_until, now)
            paid_until = base + timedelta(days=self.settings.subscription_days)
        else:
            paid_until = now + timedelta(days=self.settings.subscription_days)

        await self.db.activate(tg_id, paid_until, source="prodamus", paid_at=now)
        if order_currency != "rub":
            # оплата в валюте — разовая: автопродления нет, продление вручную
            await self.db.set_auto_renew(tg_id, False)
            await self.db.set_last_currency(tg_id, order_currency)
        if order is not None:
            await self.db.set_order_status(order["order_id"], "paid")
            if order.get("promo_code"):
                await self.db.use_promo(order["promo_code"])

        paid_amount = note.amount or (order["amount"] if order else self.settings.price)
        if is_renewal:
            await self.access.send_renewed(tg_id, paid_until)
        else:
            await self.access.send_access(tg_id, paid_until=paid_until)

        await self.access.notify_payment(
            tg_id,
            paid_amount,
            currency=order_currency,
            renewal=is_renewal,
            promo_code=(order or {}).get("promo_code"),
            username=(user or {}).get("username"),
        )
        log.info(
            "Оплата засчитана: tg_id=%s, %s, продление=%s, доступ до %s",
            tg_id,
            fmt_price(paid_amount, order_currency),
            is_renewal,
            paid_until.isoformat(),
        )
        return "renewed" if is_renewal else "paid"

    # ------------------------------------------------------------------ помощники

    async def _resolve_user(self, note: Notification) -> int | None:
        """Ищем tg_id: заказ -> customer_extra -> телефон/почта."""
        if note.order_id:
            order = await self.db.get_order(note.order_id)
            if order:
                return int(order["tg_id"])
        if note.tg_id:
            return int(note.tg_id)
        if note.phone or note.email:
            user = await self.db.get_user_by_contact(note.phone, note.email)
            if user:
                return int(user["tg_id"])
        return None

    async def grant_manually(
        self, tg_id: int, days: int | None = None
    ) -> tuple[bool, datetime, str | None]:
        """Ручная выдача доступа (подарок, конкурс, оплата мимо Prodamus)."""
        days = days or self.settings.subscription_days
        user = await self.db.get_user(tg_id) or await self.db.upsert_user(tg_id)
        now = utcnow()
        current_until = from_iso(user.get("paid_until"))
        # если доступ ещё действует — продлеваем от текущей даты окончания
        base = current_until if current_until and current_until > now else now
        paid_until = base + timedelta(days=days)
        await self.db.activate(tg_id, paid_until, source="manual", paid_at=now)
        ok, error = await self.access.send_access(tg_id, manual=True, paid_until=paid_until)
        return ok, paid_until, error


__all__ = ["PaymentProcessor"]
