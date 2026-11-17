"""Обработка платежей: вебхуки Prodamus, выдача и продление доступа."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from html import escape
from urllib.parse import urlsplit

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

        return await self._create_dynamic_link(tg_id, discount=discount, promo_code=promo_code)

    async def _create_dynamic_link(
        self, tg_id: int, *, discount: int = 0, promo_code: str | None = None,
        one_time: bool = False,
    ) -> tuple[str, int, str, int]:
        """Общий механизм для обычной оплаты, /pay1 и динамической зарубежной формы."""
        discount = max(0, min(discount, self.settings.price))
        amount = self.settings.price - discount
        order_id = self.new_order_id(tg_id)
        url = self.prodamus.payment_url(
            tg_id, order_id, self.settings.price, discount,
            currency="rub" if one_time else None, one_time=one_time,
        )
        await self.db.create_order(
            order_id=order_id,
            tg_id=tg_id,
            amount=amount,
            promo_code=promo_code,
            is_recurring=self.settings.is_recurrent and not one_time,
            currency="foreign" if one_time else "rub",
            checkout_url=url,
            checkout_token=uuid.uuid4().hex,
        )
        return url, amount, order_id, discount

    async def create_test_payment_link(self, tg_id: int) -> tuple[str, int, str, int]:
        """/pay1 проходит тот же путь, что и основная оплата, со скидкой до 1 ₽."""
        return await self._create_dynamic_link(tg_id, discount=max(0, self.settings.price - 1))

    async def create_foreign_link(self, tg_id: int) -> tuple[str, int, str, str]:
        """Разовая оплата для покупателей из других стран.

        По умолчанию отдаём настроенную постоянную ссылку. Опционально
        создаём разовый динамический заказ на основной форме тем же способом,
        что и /pay1. Возможность оплаты зарубежной картой и возврат номера заказа
        на этой форме обязательно проверяются отдельно в кабинете Prodamus.
        """
        if not self.settings.prodamus_foreign_url:
            raise ValueError("Ссылка для оплаты из-за рубежа не настроена")
        if self.settings.prodamus_foreign_dynamic:
            url, amount, order_id, _ = await self._create_dynamic_link(tg_id, one_time=True)
            return url, amount, order_id, "rub"
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
            checkout_url=url,
            checkout_token=uuid.uuid4().hex,
        )
        return url, self.settings.price, order_id, "rub"

    async def checkout_button_url(self, order_id: str, direct_url: str) -> str:
        """Одно нажатие: переход через сервер бота с уведомлением и редиректом.

        Если публичный HTTPS-адрес не настроен, не ломаем оплату: возвращаем
        прямую ссылку Prodamus, но при её нажатии уведомления не будет.
        """
        base = self.settings.public_base_url or ""
        try:
            parsed = urlsplit(base)
            _port = parsed.port  # проверяем, что порт при наличии корректен
            valid = (
                parsed.scheme == "https" and parsed.hostname
                and parsed.hostname.lower() not in {"localhost", "127.0.0.1", "0.0.0.0", "::1"}
                and not parsed.username and not parsed.password
                and parsed.path in {"", "/"}
                and not parsed.query and not parsed.fragment
            )
        except ValueError:
            valid = False
        if not valid:
            return direct_url
        order = await self.db.get_order(order_id)
        if not order or not order.get("checkout_token"):
            return direct_url
        return f"{base.rstrip('/')}/checkout/{order['checkout_token']}"

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
        """Обрабатывает уведомление Prodamus. Возвращает метку результата для логов.

        Доступ выдаётся только при точном совпадении подтверждённого
        идентификатора с заказом в БД либо при однозначном совпадении
        сохранённого контакта с существующим Telegram-аккаунтом.
        Сопоставление только по сумме/времени запрещено: если надёжного
        идентификатора нет, доступ не меняется.
        """
        note = self.prodamus.parse(data)
        # Дубликат по внутреннему ID Prodamus: тот же платёж уже привязан
        # к пользователю и обработан. Несопоставленные платежи не блокируем —
        # повтор с надёжным ID должен обработаться.
        if (
            note.kind == "paid"
            and note.prodamus_order_id
            and await self.db.has_linked_successful_payment(note.prodamus_order_id)
        ):
            log.info("Повторное подтверждённое уведомление Prodamus — пропускаем")
            return "duplicate"

        order = await self._find_order(data, note)
        order_currency = str((order or {}).get("currency") or "rub").lower()
        paid_currency = (note.charged_currency or "").lower()
        # платёж из-за рубежа: валютный счёт или бессрочная ссылка с конвертацией
        is_foreign = order_currency not in {"rub"} or paid_currency not in {"", "rub"}
        # Для отображения используем фактически списанную сумму (currency_sum),
        # а не сумму заказа (sum). 790.00 RUB нельзя показывать как 790 $.
        if note.charged_amount is not None and paid_currency:
            display_amount: float = note.charged_amount
            display_currency = paid_currency
        elif note.order_sum is not None:
            display_amount = note.order_sum
            display_currency = "rub"
        else:
            display_amount = note.amount or (order["amount"] if order else self.settings.price)
            display_currency = "rub"
        # Сумма из sum — базовая сумма заказа; не подставляем цену тарифа,
        # если сумма отсутствует: скидки и неполные уведомления возможны.
        stats_amount = note.order_sum

        tg_id = await self._resolve_user(note, order)
        if note.kind != "paid" or tg_id is None:
            await self.db.add_payment(
                order_id=note.prodamus_order_id, tg_id=tg_id, amount=stats_amount,
                kind=note.kind, is_renewal=note.is_renewal,
                status_text=note.status_text, raw=data,
            )
        if tg_id is None:
            # Не угадываем пользователя по сумме/времени и не меняем доступ.
            # В уведомление админу не включаем телефон/email и сырое тело.
            log.warning("Не удалось определить пользователя из уведомления Prodamus")
            await self.access.notify_admins(
                "⚠️ <b>Платёж пришёл, но пользователя не удалось определить.</b>\n\n"
                f"order_id Prodamus: <code>{escape(str(note.prodamus_order_id or '—'))}</code>\n"
                f"order_num: <code>{escape(str(note.order_id or '—'))}</code>\n"
                f"Сумма заказа: {fmt_price(note.order_sum, 'rub') if note.order_sum is not None else '—'}\n"
                + (
                    f"Списано: {fmt_price(display_amount, display_currency)}\n"
                    if note.charged_amount is not None and paid_currency
                    else ""
                )
                + "\nДоступ не выдан автоматически. Проверьте платёж в кабинете Prodamus "
                "и выдайте доступ вручную: /admin → «Выдать доступ»."
            )
            return "unknown-user"

        user = await self.db.get_user(tg_id)
        if user is None:
            await self.access.notify_admins(
                "⚠️ Заказ найден, но пользователь отсутствует в БД. Доступ не изменён; нужна проверка."
            )
            return "unknown-user"

        if note.kind == "failed":
            if order is not None:
                await self.db.set_order_status(order["order_id"], "failed")
            await self.access.send_failure(tg_id)
            log.info("Неуспешная оплата у %s", tg_id)
            return "failed"

        if note.kind == "cancelled":
            await self.db.set_auto_renew(tg_id, False, cancelled=True)
            log.info("Подписка %s завершена/отменена", tg_id)
            await self.access.notify_admins(
                "🔁 <b>Подписка отменена на стороне Prodamus</b>\n\n"
                f"Пользователь: {human_username(user)}\n"
                f"ID: <code>{tg_id}</code>\n"
                "Причина: отписка / завершение подписки\n\n"
                "Автосписание в базе бота выключено, доступ закроется в конце периода."
            )
            return "subscription-cancelled"

        if note.kind != "paid":
            log.info("Информационное уведомление Prodamus")
            return "info"

        # Без стабильного ID платежа невозможно безопасно распознать повтор.
        if not note.prodamus_order_id:
            await self.access.notify_admins(
                "⚠️ Платёж без ID Prodamus: автоматическая выдача невозможна."
            )
            return "missing-payment-id"

        applied, paid_until, is_renewal = await self.db.apply_paid_payment(
            prodamus_id=note.prodamus_order_id,
            order_id=order["order_id"] if order else None,
            tg_id=tg_id, amount=stats_amount, status_text=note.status_text, raw=data,
            days=self.settings.subscription_days, is_foreign=is_foreign,
            currency=paid_currency or ("foreign" if is_foreign else None),
        )
        if not applied:
            return "duplicate"
        if note.phone or note.email:
            # Контакты не считаются подтверждёнными автоматически.
            await self.db.save_contacts(tg_id, note.phone, note.email)

        if is_renewal:
            await self.access.send_renewed(tg_id, paid_until)
        else:
            await self.access.send_access(tg_id, paid_until=paid_until)

        await self.access.notify_payment(
            tg_id,
            display_amount,
            currency=display_currency,
            renewal=is_renewal,
            promo_code=(order or {}).get("promo_code"),
            username=(user or {}).get("username"),
        )
        log.info(
            "Оплата засчитана: tg_id=%s, %s, продление=%s, доступ до %s",
            tg_id,
            fmt_price(display_amount, display_currency),
            is_renewal,
            paid_until.isoformat(),
        )
        return "renewed" if is_renewal else "paid"

    # ------------------------------------------------------------------ помощники

    async def _find_order(self, data: dict, note: Notification) -> dict | None:
        # Только order_num — номер магазина; order_id принадлежит Prodamus.
        if note.order_id and note.extra_order_id and note.order_id != note.extra_order_id:
            return None
        order_id = note.order_id or note.extra_order_id
        if order_id:
            return await self.db.get_order(order_id)
        if note.tg_id:
            # Telegram ID может указывать на несколько заказов: не выбираем
            # последний и не используем сумму или время для выбора.
            rows = await self.db._fetchall(
                "SELECT * FROM orders WHERE tg_id = ? AND status = 'pending' LIMIT 2",
                (note.tg_id,),
            )
            return rows[0] if len(rows) == 1 else None
        return None

    async def _resolve_user(
        self, note: Notification, order: dict | None = None
    ) -> int | None:
        if order is not None:
            if note.tg_id and note.tg_id != int(order["tg_id"]):
                return None
            return int(order["tg_id"])
        # Если указан неизвестный номер заказа или Telegram ID, не подменяем
        # его контактом: конфликт требует проверки оператором.
        if note.order_id or note.extra_order_id or note.tg_id:
            return None
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
