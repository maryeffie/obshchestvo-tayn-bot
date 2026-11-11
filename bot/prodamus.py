r"""Интеграция с Prodamus: платёжные ссылки, подпись, разбор уведомлений.

Формат подписи (документация Prodamus + проверено на живой форме):
  1. все значения приводятся к строкам;
  2. массив сортируется по ключам вглубь;
  3. массив превращается в JSON-строку;
  4. в JSON-строке экранируется «/»;
  5. строка подписывается HMAC-SHA256 секретным ключом платёжной страницы.

Важная деталь, найденная опытным путём: кириллица в JSON должна оставаться
UTF-8 — если экранировать её как \uXXXX (поведение PHP json_encode по
умолчанию), Prodamus считает подпись неверной и отбрасывает параметры ссылки
(редирект на корень платёжной формы). Поэтому используем ensure_ascii=False
+ экранирование «/», как в Hmac.php с JSON_UNESCAPED_UNICODE.
Проверить подпись на своей форме: python scripts/check_prodamus.py

При проверке входящих уведомлений принимаем все варианты представления —
это страхует от различий в реализациях на стороне сервисов-посредников.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Iterable
from urllib.parse import urlencode

import aiohttp

from .config import Settings

log = logging.getLogger(__name__)

PAID_STATUSES = {"success", "succeeded", "paid", "ok", "completed", "done"}
FAILED_STATUSES = {
    "fail",
    "failed",
    "failure",
    "error",
    "declined",
    "canceled",
    "cancelled",
    "rejected",
    "refused",
}
CANCELLED_STATUSES = {
    "expired",
    "finished",
    "deactivated",
    "inactive",
    "unsubscribed",
    "closed",
    "stopped",
}

TG_IN_EXTRA = re.compile(r"tg[:=](\d+)", re.IGNORECASE)
TG_IN_ORDER = re.compile(r"^tg(\d+)-", re.IGNORECASE)


# --------------------------------------------------------------------- подпись

def _json_candidates(payload: Any) -> list[str]:
    """Все разумные варианты JSON-представления для проверки подписи."""
    bodies = {
        json.dumps(payload, ensure_ascii=flag, separators=(",", ":"), sort_keys=True)
        for flag in (False, True)
    }
    return list(bodies | {body.replace("/", "\\/") for body in bodies})


def _stringify(value: Any) -> Any:
    """Приводим все значения к строкам, как требует Prodamus."""
    if isinstance(value, dict):
        return {str(k): _stringify(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_stringify(v) for v in value]
    if isinstance(value, bool):
        return "1" if value else "0"
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


def sign_payload(
    payload: dict,
    secret: str,
    *,
    escape_slashes: bool = True,
    ensure_ascii: bool = False,
) -> str:
    """Считает подпись для исходящего запроса.

    По умолчанию — вариант, который принимает Prodamus: UTF-8 JSON
    с экранированными «/». Параметры нужны для диагностики
    (scripts/check_prodamus.py перебирает варианты на живой форме).
    """
    normalized = _stringify(payload)
    body = json.dumps(
        normalized, ensure_ascii=ensure_ascii, separators=(",", ":"), sort_keys=True
    )
    if escape_slashes:
        body = body.replace("/", "\\/")
    return hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()


def verify_signature(payload: Any, secret: str, signature: str | None) -> bool:
    """Проверяет подпись входящего уведомления."""
    if not signature:
        return False
    signature = signature.strip().strip('"').lower()
    normalized = _stringify(payload)
    for body in _json_candidates(normalized):
        digest = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
        if hmac.compare_digest(digest, signature):
            return True
    return False


# ------------------------------------------------------------------ уведомление

@dataclass
class Notification:
    kind: str                      # paid | failed | cancelled | info
    order_id: str | None = None
    tg_id: int | None = None
    amount: int | None = None
    currency: str | None = None
    phone: str | None = None
    email: str | None = None
    is_subscription: bool = False
    is_renewal: bool = False
    status_text: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def is_money_event(self) -> bool:
        return self.kind in {"paid", "failed"}


def _first(data: dict, *keys: str) -> Any:
    for key in keys:
        value = data.get(key)
        if value not in (None, ""):
            return value
    return None


def _to_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(round(float(str(value).replace(" ", "").replace(",", "."))))
    except ValueError:
        return None


@dataclass
class SubscriptionResult:
    ok: bool
    message: str
    tried: str | None = None      # каким идентификатором пробовали
    remote: bool = True           # удалось ли реально достучаться до Prodamus

class ProdamusClient:
    """Клиент Prodamus: ссылки, подпись, разбор уведомлений, Rest API подписок."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    # ---------------------------------------------------------------- ссылка

    def payment_url(
        self,
        tg_id: int,
        order_id: str,
        base_amount: int,
        discount: int = 0,
        currency: str | None = None,
    ) -> str:
        """Формирует ссылку на платёжную форму.

        В рекуррентном режиме сумма берётся из настроек подписного товара,
        скидка передаётся параметром discount_value. В разовом режиме цена
        подставляется в products.
        """
        discount = max(0, min(discount, base_amount))
        currency = (currency or self.settings.currency or "rub").lower()
        # подписка в Prodamus бывает только в рублях, поэтому оплата в валюте —
        # всегда разовая, без параметра subscription
        foreign = currency != "rub"
        data: dict[str, Any] = {
            "do": "pay",
            "order_id": order_id,
            # страховка: даже если order_id потеряется, по этому полю найдём человека
            "customer_extra": f"tg:{tg_id}",
        }
        if foreign:
            data["currency"] = currency
        if self.settings.prodamus_sys:
            data["sys"] = self.settings.prodamus_sys
        if self.settings.prodamus_send_tg_user_id:
            # по этому полю Prodamus сам привяжет подписку к нашему пользователю
            data["tg_user_id"] = str(tg_id)
        if self.settings.prodamus_url_success:
            data["urlSuccess"] = self.settings.prodamus_url_success
        if self.settings.prodamus_url_return:
            data["urlReturn"] = self.settings.prodamus_url_return

        if self.settings.is_recurrent and not foreign:
            data["subscription"] = str(self.settings.prodamus_subscription_id)
            if discount:
                data["discount_value"] = str(discount)
            data["products"] = [
                {
                    "name": self.settings.product_name,
                    "price": str(base_amount),
                    "quantity": "1",
                    "type": "service",
                }
            ]
        else:
            data["products"] = [
                {
                    "name": self.settings.product_name,
                    "price": str(base_amount - discount),
                    "quantity": "1",
                    "type": "service",
                }
            ]

        if self.settings.prodamus_sign_links:
            data["signature"] = sign_payload(data, self.settings.prodamus_secret)

        return self.settings.prodamus_form_url + "?" + urlencode(list(_flatten(data)))

    def foreign_payment_url(self, tg_id: int, order_id: str) -> str | None:
        """Бессрочная ссылка Prodamus: счёт в рублях, платёж в валюте по курсу банка.

        Ссылку создаёт Марина в кабинете (PRODAMUS_FOREIGN_URL). Мы только
        добавляем к ней свои параметры, чтобы узнать человека по уведомлению.
        """
        base = self.settings.prodamus_foreign_url
        if not base:
            return None
        # минимум параметров: всё лишнее Prodamus может не принять на бессрочной ссылке
        data: dict[str, Any] = {
            "order_id": order_id,
            "customer_extra": f"tg:{tg_id}",
        }
        if self.settings.prodamus_url_success:
            data["urlSuccess"] = self.settings.prodamus_url_success
        if self.settings.prodamus_url_return:
            data["urlReturn"] = self.settings.prodamus_url_return
        # Подпись сюда НЕ добавляем: на бессрочных ссылках Prodamus проверяет её
        # по своему алгоритму и отвечает «Ошибка подписи передаваемых данных».
        # По документации параметр signature для ссылки необязателен.
        separator = "&" if "?" in base else "?"
        return base + separator + urlencode(list(_flatten(data)))

    # -------------------------------------------------------------- проверка

    def verify(self, payload: dict, signature: str | None) -> bool:
        if verify_signature(payload, self.settings.prodamus_secret, signature):
            return True
        # Демо-подпись намеренно отличается: такие платежи принимать нельзя.
        if verify_signature(payload, self.settings.prodamus_secret + "demo", signature):
            log.warning(
                "Уведомление подписано демо-ключом Prodamus — платеж НЕ засчитан. "
                "Проверьте, что на стороне Prodamus включён боевой режим."
            )
        return False

    def verify_request(self, form: dict, headers: dict, raw_body: bytes | None = None) -> bool:
        """Проверяет подпись с учётом разных вариантов тела запроса."""
        signature = _header(headers, "Sign") or form.get("signature") or form.get("sign")
        candidates: list[dict] = []

        submit = form.get("submit")
        if isinstance(submit, str):
            try:
                parsed = json.loads(submit)
                if isinstance(parsed, dict):
                    candidates.append(parsed)
            except json.JSONDecodeError:
                pass
        elif isinstance(submit, dict):
            candidates.append(submit)

        candidates.append(form)
        candidates.append({k: v for k, v in form.items() if k not in {"signature", "sign"}})

        for candidate in candidates:
            if candidate and self.verify(candidate, signature):
                return True
        return False

    # --------------------------------------------------------------- разбор

    def parse(self, data: dict) -> Notification:
        payment_status = str(_first(data, "payment_status", "status") or "").strip().lower()
        sub_status = str(
            _first(data, "subscription_status", "subscription_state") or ""
        ).strip().lower()
        description = str(
            _first(data, "payment_status_description", "status_description", "description") or ""
        )
        action_code = str(_first(data, "action_code") or "").strip().lower()
        notification_code = str(_first(data, "notification_code") or "").strip().lower()
        error_code = _first(data, "error_code")
        active_flag = str(_first(data, "active") or "").strip().lower()
        subscription_id = _first(data, "subscription", "subscription_id")
        order_id = _first(data, "order_id", "order_num")
        order_id = str(order_id) if order_id not in (None, "") else None

        extra = str(_first(data, "customer_extra", "customer_note") or "")
        tg_id = None
        match = TG_IN_EXTRA.search(extra)
        if match:
            tg_id = int(match.group(1))
        elif order_id:
            match = TG_IN_ORDER.match(order_id)
            if match:
                tg_id = int(match.group(1))

        amount = _to_int(
            _first(data, "sum", "order_sum", "payment_sum", "amount", "products[0][price]")
        )
        if amount is None and isinstance(data.get("products"), list) and data["products"]:
            amount = _to_int(data["products"][0].get("price"))

        kind = "info"
        if payment_status in PAID_STATUSES:
            kind = "paid"
        elif payment_status in FAILED_STATUSES or "недостаточно средств" in description.lower():
            kind = "failed"
        elif sub_status in PAID_STATUSES:
            kind = "paid"
        elif sub_status in FAILED_STATUSES:
            kind = "failed"
        elif sub_status in CANCELLED_STATUSES:
            kind = "cancelled"
        elif action_code in {"deactivation", "finish"}:
            # отписка пользователя / менеджера или завершение подписки
            kind = "cancelled"
        elif action_code == "auto_payment":
            kind = "failed" if error_code or active_flag in {"0", "false"} else "paid"
        elif notification_code == "auto_payment":
            kind = "failed" if error_code else "paid"
        elif active_flag in {"0", "false"} and _first(data, "subscription", "id"):
            kind = "cancelled"

        is_renewal = bool(data.get("subscription_id") or data.get("parent_order_id"))
        if not is_renewal and order_id:
            is_renewal = "recurr" in order_id.lower() or "auto" in order_id.lower()
        if not is_renewal and sub_status and payment_status:
            # повторные списания обычно приходят без products
            is_renewal = not data.get("products")

        return Notification(
            kind=kind,
            order_id=order_id,
            tg_id=tg_id,
            amount=amount,
            currency=(str(_first(data, "currency", "currency_code") or "") or None),
            phone=_first(data, "customer_phone"),
            email=_first(data, "customer_email"),
            is_subscription=bool(subscription_id),
            is_renewal=is_renewal,
            status_text=description or payment_status or sub_status,
            raw=data,
        )


    def rest_url(self, method: str) -> str:
        return f"{self.settings.prodamus_form_url}rest/{method}/"

    async def set_subscription_activity(
        self,
        *,
        active: bool,
        as_manager: bool = False,
        tg_user_id: int | None = None,
        phone: str | None = None,
        email: str | None = None,
        timeout: int = 15,
    ) -> SubscriptionResult:
        """Включает/выключает автосписание.

        as_manager=False — отписка «от лица пользователя» (то, что делает сам участник
        кнопкой в боте). Её нельзя отменить через API, вернуться можно только новой оплатой.
        as_manager=True — отписка менеджером (делает Марина из админки), её можно включить обратно.
        """
        if not self.settings.is_recurrent:
            return SubscriptionResult(
                ok=False,
                message="Подписка не настроена (нет PRODAMUS_SUBSCRIPTION_ID)",
                remote=False,
            )

        attempts: list[tuple[str, dict]] = []
        if tg_user_id:
            attempts.append((f"tg_user_id={tg_user_id}", {"tg_user_id": str(tg_user_id)}))
        if phone:
            attempts.append((f"customer_phone={phone}", {"customer_phone": phone}))
        if email:
            attempts.append((f"customer_email={email}", {"customer_email": email}))
        if not attempts:
            return SubscriptionResult(ok=False, message="Нет идентификатора клиента", remote=False)

        last_error = "неизвестная ошибка"
        for label, identifier in attempts:
            data: dict[str, Any] = {
                "subscription": str(self.settings.prodamus_subscription_id),
                **identifier,
                "active_manager" if as_manager else "active_user": "1" if active else "0",
            }
            data["signature"] = sign_payload(data, self.settings.prodamus_secret)
            try:
                async with aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=timeout)
                ) as session:
                    async with session.post(self.rest_url("setActivity"), data=data) as response:
                        body = (await response.text()).strip()
                if "success" in body.lower():
                    return SubscriptionResult(ok=True, message=body, tried=label)
                last_error = f"HTTP {response.status}: {body[:200]}"
                log.warning("setActivity (%s) вернул: %s", label, last_error)
            except Exception as exc:  # noqa: BLE001 — сеть может быть недоступна
                last_error = f"{type(exc).__name__}: {exc}"
                log.warning("setActivity (%s) не удался: %s", label, last_error)

        return SubscriptionResult(ok=False, message=last_error, tried=attempts[-1][0])


def _header(headers: dict, name: str) -> str | None:
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    return None


def _flatten(data: dict, parent: str | None = None) -> Iterable[tuple[str, str]]:
    """PHP-подобное преобразование вложенного dict в пары для query string."""
    for key, value in data.items():
        full = f"{parent}[{key}]" if parent else str(key)
        if isinstance(value, dict):
            yield from _flatten(value, full)
        elif isinstance(value, (list, tuple)):
            for index, item in enumerate(value):
                indexed = f"{full}[{index}]"
                if isinstance(item, dict):
                    yield from _flatten(item, indexed)
                else:
                    yield indexed, str(item)
        else:
            yield full, "" if value is None else str(value)


__all__ = [
    "ProdamusClient",
    "Notification",
    "SubscriptionResult",
    "sign_payload",
    "verify_signature",
    "PAID_STATUSES",
    "FAILED_STATUSES",
]
