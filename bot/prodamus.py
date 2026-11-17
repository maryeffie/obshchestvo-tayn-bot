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
import math
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
    # order_id — номер заказа в нашей системе (merchant order number).
    # В webhook Prodamus возвращает его как order_num, а свой внутренний
    # числовой ID — как order_id. Внутренний ID никогда не является ID
    # заказа бота и не используется для поиска в БД.
    order_id: str | None = None
    prodamus_order_id: str | None = None  # внутренний ID Prodamus (webhook order_id)
    extra_order_id: str | None = None  # только точный маркер order:<локальный ID>
    tg_id: int | None = None
    amount: int | None = None      # округлённая сумма заказа, для совместимости
    order_sum: float | None = None  # сумма заказа (webhook sum) без округления
    charged_amount: float | None = None  # фактически списанная сумма (webhook currency_sum)
    charged_currency: str | None = None  # валюта списания (webhook currency)
    currency: str | None = None    # валюта списания (алиас charged_currency)
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


def _to_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        number = float(str(value).replace(" ", "").replace(",", "."))
        return number if math.isfinite(number) and number >= 0 else None
    except (ValueError, TypeError):
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
        *,
        one_time: bool = False,
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
            # По документации Prodamus параметр ссылки order_id — номер заказа
            # в нашей системе. В webhook Prodamus возвращает его как order_num,
            # а свой внутренний числовой ID — как order_id. Именно order_num
            # является стабильным ключом для поиска локального заказа.
            # Параметр order_num в ссылке не документирован и не передаётся.
            "order_id": order_id,
            # Резервный точный номер заказа, не просто Telegram ID. Форма может
            # перезаписать примечание; возврат необходимо подтвердить webhook-ом.
            "customer_extra": f"order:{order_id}",
        }
        if foreign:
            data["currency"] = currency
        if self.settings.prodamus_sys:
            data["sys"] = self.settings.prodamus_sys
        # tg_user_id в документации платёжной ссылки не описан как возвращаемое
        # поле webhook (он документирован только для Rest API setActivity).
        # Не передаём его в ссылку и не полагаемся на него для сопоставления.
        if self.settings.prodamus_url_success:
            data["urlSuccess"] = self.settings.prodamus_url_success
        if self.settings.prodamus_url_return:
            data["urlReturn"] = self.settings.prodamus_url_return

        if self.settings.is_recurrent and not foreign and not one_time:
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
        # Передаём только документированные поля: order_id (номер заказа в нашей
        # системе, возвращается как order_num) и customer_extra (возвращается,
        # но на бессрочной ссылке может перезаписываться текстом формы).
        # Недокументированные order_num/tg_user_id в ссылке не передаём.
        # ВНИМАНИЕ: добавление параметра в URL само по себе не гарантирует его
        # возврата — это должно подтвердить тестовый webhook на живой форме.
        data: dict[str, Any] = {
            "order_id": order_id,
            "customer_extra": f"order:{order_id}",
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

    def _sign_variants(self, data: Any) -> list[bytes]:
        """Все разумные способы сериализовать данные для подписи.

        Prodamus подписывает JSON своим (PHP-шным) способом, и заранее неизвестно,
        сортирует ли он ключи и экранирует ли слэши. Пробуем весь набор —
        совпадение хотя бы с одним означает, что уведомление подлинное.
        """
        variants: list[bytes] = []
        for sort_keys in (True, False):
            for ensure_ascii in (False, True):
                for escape_slashes in (False, True):
                    try:
                        text = json.dumps(
                            data,
                            ensure_ascii=ensure_ascii,
                            separators=(",", ":"),
                            sort_keys=sort_keys,
                        )
                    except (TypeError, ValueError):
                        continue
                    if escape_slashes:
                        text = text.replace("/", "\\/")
                    variants.append(text.encode("utf-8"))
        return variants

    @staticmethod
    def _unflatten(data: dict) -> dict:
        """products[0][name] -> {"products": [{"name": ...}]}.

        В urlencoded-уведомлении Prodamus корзина приходит «плоскими» ключами,
        а подписывается вложенным JSON. Восстанавливаем структуру.
        """
        root: dict = {}
        for key, value in data.items():
            parts = re.findall(r"[^\[\]]+", str(key))
            if len(parts) < 2:
                root[key] = value
                continue
            node: Any = root
            for index, part in enumerate(parts):
                last = index == len(parts) - 1
                slot: Any = int(part) if part.isdigit() else part
                if last:
                    if isinstance(node, list):
                        while len(node) <= slot:
                            node.append(None)
                        node[slot] = value
                    else:
                        node[slot] = value
                    break
                next_is_index = parts[index + 1].isdigit()
                if isinstance(node, list):
                    while len(node) <= slot:
                        node.append(None)
                    if not isinstance(node[slot], (dict, list)):
                        node[slot] = [] if next_is_index else {}
                    node = node[slot]
                else:
                    if not isinstance(node.get(slot), (dict, list)):
                        node[slot] = [] if next_is_index else {}
                    node = node[slot]
        return root

    def verify_raw(self, raw_body: bytes, signature: str | None, data: dict) -> bool:
        """Проверка подписи по сырому телу запроса и по всем вариантам сериализации."""
        if not signature:
            return False
        expected = str(signature).strip().lower()
        secret = self.settings.prodamus_secret.encode("utf-8")

        candidates: list[bytes] = [raw_body]
        cleaned = {k: v for k, v in data.items() if k not in {"signature", "sign"}}
        candidates.extend(self._sign_variants(cleaned))
        nested = self._unflatten(cleaned)
        if nested != cleaned:
            candidates.extend(self._sign_variants(nested))

        for candidate in candidates:
            digest = hmac.new(secret, candidate, hashlib.sha256).hexdigest()
            if hmac.compare_digest(digest, expected):
                return True
        return False

    def verify_request(self, form: dict, headers: dict, raw_body: bytes | None = None) -> bool:
        """Проверяет подпись с учётом разных вариантов тела запроса."""
        signature = _header(headers, "Sign") or form.get("signature") or form.get("sign")
        # Проверяем именно поля, которые затем будут обработаны, а не вложенный
        # submit с независимой подписью и неподписанные поля снаружи.
        candidates = [form, {k: v for k, v in form.items() if k not in {"signature", "sign"}}]

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
        # По документации Prodamus:
        # - webhook order_id — внутренний ID заказа в системе Prodamus;
        # - webhook order_num — номер заказа на стороне магазина (значение,
        #   которое мы передавали как order_id в платёжной ссылке).
        # Внутренний ID никогда не является ID заказа бота.
        prodamus_order_id = _first(data, "order_id")
        prodamus_order_id = (
            str(prodamus_order_id) if prodamus_order_id not in (None, "") else None
        )
        merchant_order_num = _first(data, "order_num")
        merchant_order_num = (
            str(merchant_order_num) if merchant_order_num not in (None, "") else None
        )
        # order_id уведомления — merchant-номер (для поиска в БД). Если order_num
        # пуст (например, автоплатёж подписки), оставляем None: внутренний ID
        # Prodamus как ID заказа бота не используем.
        order_id = merchant_order_num
        # Только наш полный уникальный номер заказа, не поиск цифр в тексте.
        # Произвольные примечания (включая старый tg:<id>) не сопоставляются.
        extra = str(_first(data, "customer_extra") or "").strip()
        marker = re.fullmatch(r"order:(tg[1-9][0-9]*-[0-9a-f]{12})", extra)
        extra_order_id = marker.group(1) if marker else None

        # tg_user_id не документирован для платёжных webhook. Если он пришёл,
        # используем его только для точного поиска заказа, не как самостоятельное
        # доказательство оплаты или извлечения ID из текста формы.
        tg_id = None
        raw_tg_id = _first(data, "tg_user_id")
        if raw_tg_id is not None and str(raw_tg_id).strip().isdigit():
            candidate = int(str(raw_tg_id).strip())
            if candidate > 0:
                tg_id = candidate

        # sum — сумма заказа (в рублях). currency_sum — фактически списанная
        # сумма в валюте (currency). Их нельзя смешивать: 790.00 RUB != 790 USD.
        order_sum = _to_float(
            _first(data, "sum", "order_sum", "payment_sum", "amount", "products[0][price]")
        )
        if order_sum is None and isinstance(data.get("products"), list) and data["products"]:
            order_sum = _to_float(data["products"][0].get("price"))
        amount = int(round(order_sum)) if order_sum is not None else None
        charged_amount = _to_float(_first(data, "currency_sum"))
        charged_currency = _first(data, "currency", "currency_code")
        charged_currency = (
            str(charged_currency).strip().lower() if charged_currency not in (None, "") else None
        )

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
            prodamus_order_id=prodamus_order_id,
            extra_order_id=extra_order_id,
            tg_id=tg_id,
            amount=amount,
            order_sum=order_sum,
            charged_amount=charged_amount,
            charged_currency=charged_currency,
            currency=charged_currency,
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
            attempts.append(("customer_phone", {"customer_phone": phone}))
        if email:
            attempts.append(("customer_email", {"customer_email": email}))
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
                    return SubscriptionResult(ok=True, message="success", tried=label)
                last_error = f"HTTP {response.status}: запрос отклонён"
                log.warning("setActivity (%s) вернул: %s", label, last_error)
            except Exception as exc:  # noqa: BLE001 — сеть может быть недоступна
                last_error = f"{type(exc).__name__}: ошибка запроса"
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
