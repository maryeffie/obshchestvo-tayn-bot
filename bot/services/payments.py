"""Обработка платежей: вебхуки Prodamus, выдача и продление доступа."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta
from html import escape

from ..config import Settings
from ..db import Database
from ..prodamus import Notification, ProdamusClient
from ..utils import fmt_price, from_iso, human_username, to_iso, utcnow
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

    async def create_foreign_link(self, tg_id: int) -> tuple[str, int, str, str]:
        """Разовая оплата для покупателей из других стран.

        Отдаём бессрочную ссылку Prodamus: сумма пересчитывается в валюту
        плательщика по курсу банка на день оплаты, поэтому своих цен в валюте
        мы не держим.

        Подписка с автосписанием в Prodamus возможна только в рублях и только
        по картам РФ, поэтому для зарубежных платежей — только разовый счёт.
        """
        order_id = self.new_order_id(tg_id)
        url = self.prodamus.foreign_payment_url(tg_id, order_id)
        if url is None:
            raise ValueError("Ссылка для оплаты из-за рубежа не настроена")
        await self.db.create_order(
            order_id=order_id,
            tg_id=tg_id,
            amount=self.settings.price,
            is_recurring=False,
            currency="foreign",
        )
        return url, self.settings.price, order_id, "rub"

    async def create_link_auto(self, tg_id: int) -> tuple[str, int, str, str]:
        """Ссылка для «Продлить»: та же дорожка, по которой человек платил в прошлый раз."""
        user = await self.db.get_user(tg_id) or {}
        currency = str(user.get("last_currency") or "rub").lower()
        if currency != "rub":
            try:
                return await self.create_foreign_link(tg_id)
            except ValueError:
                log.warning("Не удалось собрать зарубежную ссылку, отдаю рублёвую")
        url, amount, order_id, _discount = await self.create_payment_link(tg_id)
        return url, amount, order_id, "rub"

    # ------------------------------------------------------------ обработка вебхука

    async def handle(self, data: dict) -> str:
        """Обрабатывает уведомление Prodamus. Возвращает метку результата для логов."""
        note = self.prodamus.parse(data)
        if (
            note.kind == "paid"
            and note.order_id
            and await self.db.has_linked_successful_payment(note.order_id)
        ):
            log.info("Повторное подтверждённое уведомление Prodamus %s — пропускаем", note.order_id)
            return "duplicate"

        order = await self._find_order(data, note)
        order_currency = str((order or {}).get("currency") or "rub").lower()
        paid_currency = (note.currency or "").lower()
        # платёж из-за рубежа: валютный счёт или бессрочная ссылка с конвертацией
        is_foreign = order_currency not in {"rub"} or paid_currency not in {"", "rub"}
        display_currency = paid_currency or (
            order_currency if order_currency != "foreign" else "rub"
        )
        # в статистике всё считаем в рублях: один оплаченный месяц = цена подписки
        stats_amount = self.settings.price if is_foreign else note.amount
        # Prodamus может прислать внутренний числовой order_id, а наш заказ
        # найтись только по order_num. Используем найденный заказ при привязке
        # платежа к Telegram-пользователю.
        tg_id = await self._resolve_user(note, order)
        await self.db.add_payment(
            order_id=note.order_id,
            tg_id=tg_id,
            amount=stats_amount,
            kind=note.kind,
            is_renewal=note.is_renewal,
            status_text=note.status_text,
            raw=data,
        )
        if tg_id is None:
            log.warning(
                "Не удалось определить пользователя из уведомления "
                "(order_id=%s, order_num=%s, extra=%s)",
                note.order_id,
                data.get("order_num"),
                data.get("customer_extra"),
            )
            await self.access.notify_admins(
                "⚠️ <b>Платёж пришёл, но пользователя не удалось определить.</b>\n\n"
                f"order_id: <code>{escape(str(note.order_id or '—'))}</code>\n"
                f"order_num: <code>{escape(str(data.get('order_num') or '—'))}</code>\n"
                f"tg_user_id: <code>{escape(str(data.get('tg_user_id') or '—'))}</code>\n"
                f"customer_extra: <code>{escape(str(data.get('customer_extra') or '—'))}</code>\n"
                f"Сумма: {note.amount}\n"
                f"Телефон: {escape(str(note.phone or '—'))}\n\n"
                "Выдайте доступ вручную: /admin → «Выдать доступ»."
            )
            return "unknown-user"

        user = await self.db.get_user(tg_id)
        if user is None:
            user = await self.db.upsert_user(tg_id)

        if note.kind == "failed":
            if order is not None:
                await self.db.set_order_status(order["order_id"], "failed")
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
        if is_foreign:
            # оплата из-за рубежа — разовая: автопродления нет, продление вручную
            await self.db.set_auto_renew(tg_id, False)
            await self.db.set_last_currency(tg_id, display_currency)
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
            currency=display_currency,
            renewal=is_renewal,
            promo_code=(order or {}).get("promo_code"),
            username=(user or {}).get("username"),
        )
        log.info(
            "Оплата засчитана: tg_id=%s, %s, продление=%s, доступ до %s",
            tg_id,
            fmt_price(paid_amount, display_currency),
            is_renewal,
            paid_until.isoformat(),
        )
        return "renewed" if is_renewal else "paid"

    async def reconcile_unlinked_paid_payments(self, lookback_days: int = 7) -> int:
        """Автоматически восстанавливает недавние платежи, ранее записанные без tg_id.

        Исходный webhook уже был принят обработчиком и сохранён в payments.raw.
        Перед повторной обработкой обязательно находим пользователя по заказу/контакту;
        неизвестные и неоднозначные платежи оставляем без изменений.
        """
        since = to_iso(utcnow() - timedelta(days=lookback_days))
        rows = await self.db.unlinked_paid_payments_since(since)
        recovered = 0

        for row in rows:
            try:
                data = json.loads(row["raw"])
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(data, dict):
                continue

            note = self.prodamus.parse(data)
            if note.kind != "paid":
                continue
            if note.order_id and await self.db.has_linked_successful_payment(note.order_id):
                continue

            order = await self._find_order(data, note)
            if await self._resolve_user(note, order) is None:
                continue

            result = await self.handle(data)
            if result in {"paid", "renewed"}:
                recovered += 1

        if recovered:
            log.info("Автоматически восстановлено несопоставленных платежей: %s", recovered)
        return recovered

    # ------------------------------------------------------------------ помощники

    async def _find_order(self, data: dict, note: Notification) -> dict | None:
        order = await self.db.get_order(note.order_id) if note.order_id else None
        if order is None:
            # в уведомлении Prodamus order_id — их внутренний номер, наш заказ
            # приходит в поле order_num
            for key in ("order_num", "order_id"):
                candidate = str(data.get(key) or "")
                if candidate and candidate != note.order_id:
                    order = await self.db.get_order(candidate)
                    if order is not None:
                        break
        if order is None and note.kind == "paid":
            order = await self._find_unique_recent_order(data, note)
        return order

    async def _find_unique_recent_order(
        self, data: dict, note: Notification
    ) -> dict | None:
        """Ищем ровно один свежий зарубежный заказ, если Prodamus потерял ID.

        Ограничиваем окно 15 минутами и требуем точного совпадения суммы и валютного
        типа. При нуле или нескольких кандидатах не угадываем пользователя.
        """
        if note.amount is None or (note.currency or "").lower() in {"", "rub"}:
            return None
        paid_at = from_iso(str(data.get("date") or ""))
        if paid_at is None:
            return None

        order = await self.db.find_unique_pending_order_in_window(
            amount=note.amount,
            currency="foreign",
            created_after=to_iso(paid_at - timedelta(minutes=15)),
            created_before=to_iso(paid_at + timedelta(minutes=1)),
        )
        if order is not None:
            log.warning(
                "Связали webhook %s с единственным свежим зарубежным заказом %s "
                "по сумме и времени; переданные Prodamus ID отсутствовали",
                note.order_id,
                order["order_id"],
            )
        return order

    async def _resolve_user(
        self, note: Notification, order: dict | None = None
    ) -> int | None:
        """Ищем tg_id: найденный заказ -> order_id -> customer_extra -> телефон/почта."""
        if order is not None:
            return int(order["tg_id"])
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
