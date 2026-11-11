"""Конфигурация бота «Общество тайн».

Все настройки читаются из переменных окружения (файл .env).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


class ConfigError(RuntimeError):
    """Не хватает переменной окружения или значение некорректно."""


def _raw(name: str, default: str | None = None, required: bool = False) -> str | None:
    value = os.getenv(name, default)
    if value is not None:
        value = value.strip()
    if not value:
        if required:
            raise ConfigError(
                f"Не задана обязательная переменная окружения {name}. "
                f"Проверьте файл .env (пример — .env.example)."
            )
        return None
    return value


def _int(name: str, default: int | None = None, required: bool = False) -> int | None:
    raw = _raw(name, None, required)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name}: ожидалось целое число, получено {raw!r}") from exc


def _bool(name: str, default: bool = False) -> bool:
    raw = _raw(name, None)
    if raw is None:
        return default
    return raw.lower() in {"1", "true", "yes", "y", "on", "да"}


def _admin_ids() -> tuple[int, ...]:
    raw = _raw("ADMIN_IDS", required=True)
    ids: list[int] = []
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            ids.append(int(chunk))
        except ValueError as exc:
            raise ConfigError(f"ADMIN_IDS: {chunk!r} не является числовым Telegram ID") from exc
    if not ids:
        raise ConfigError("ADMIN_IDS: укажите хотя бы один ID")
    return tuple(ids)


def _chat_ref(name: str, required: bool = True) -> int | str | None:
    """Возвращает числовой ID (int) либо @username (str)."""
    raw = _raw(name, required=required)
    if raw is None:
        return None
    candidate = raw if raw.startswith("@") else raw
    try:
        return int(candidate)
    except ValueError:
        if not candidate.startswith("@"):
            raise ConfigError(f"{name}: укажите числовой ID вида -1001234567890 или @username")
        return candidate


@dataclass(frozen=True)
class Settings:
    # Telegram
    bot_token: str
    admin_ids: tuple[int, ...]
    channel_id: int | str
    chat_id: int | str

    # Подписка
    price: int
    currency: str
    price_eur: int | None          # разовая оплата в евро (для покупателей за рубежом)
    price_usd: int | None
    price_byn: int | None
    product_name: str
    subscription_days: int
    grace_days: int
    reminder_days_before: int
    invite_link_ttl_hours: int

    # Prodamus
    prodamus_form_url: str
    prodamus_secret: str
    prodamus_subscription_id: int | None
    prodamus_sys: str | None
    prodamus_sign_links: bool
    prodamus_send_tg_user_id: bool
    prodamus_url_success: str | None
    prodamus_url_return: str | None

    # Вебхук
    webhook_host: str
    webhook_port: int
    webhook_path: str
    public_base_url: str | None

    # Прочее
    db_path: str
    timezone: ZoneInfo
    support_contact: str | None
    log_level: str

    @property
    def webhook_url(self) -> str:
        base = self.public_base_url or f"http://{self.webhook_host}:{self.webhook_port}"
        return base.rstrip("/") + self.webhook_path

    @property
    def is_recurrent(self) -> bool:
        return self.prodamus_subscription_id is not None

    def is_admin(self, user_id: int | None) -> bool:
        return user_id is not None and user_id in self.admin_ids


    @property
    def foreign_prices(self) -> dict[str, int]:
        """Валюты разовой оплаты, которые настроены (и положительны)."""
        pairs = {"eur": self.price_eur, "usd": self.price_usd, "byn": self.price_byn}
        return {code: price for code, price in pairs.items() if price and price > 0}


def load_settings() -> Settings:
    form_url = _raw("PRODAMUS_FORM_URL", "https://demo.payform.ru/", required=True)
    if not form_url.startswith("http"):
        raise ConfigError("PRODAMUS_FORM_URL должен начинаться с http:// или https://")
    if not form_url.endswith("/"):
        form_url += "/"

    return Settings(
        bot_token=_raw("BOT_TOKEN", required=True),
        admin_ids=_admin_ids(),
        channel_id=_chat_ref("CHANNEL_ID"),
        chat_id=_chat_ref("CHAT_ID"),
        price=_int("PRICE", 790),
        currency=(_raw("CURRENCY", "rub") or "rub").lower(),
        price_eur=_int("PRICE_EUR", 9),
        price_usd=_int("PRICE_USD", 10),
        price_byn=_int("PRICE_BYN", 29),
        product_name=_raw("PRODUCT_NAME", "Подписка «Общество тайн» (1 месяц)"),
        subscription_days=_int("SUBSCRIPTION_DAYS", 30),
        grace_days=_int("GRACE_DAYS", 1),
        reminder_days_before=_int("REMINDER_DAYS_BEFORE", 3),
        invite_link_ttl_hours=_int("INVITE_LINK_TTL_HOURS", 24),
        prodamus_form_url=form_url,
        prodamus_secret=_raw("PRODAMUS_SECRET", required=True),
        prodamus_subscription_id=_int("PRODAMUS_SUBSCRIPTION_ID"),
        prodamus_sys=_raw("PRODAMUS_SYS"),
        prodamus_sign_links=_bool("PRODAMUS_SIGN_LINKS", True),
        prodamus_send_tg_user_id=_bool("PRODAMUS_SEND_TG_USER_ID", True),
        prodamus_url_success=_raw("PRODAMUS_URL_SUCCESS"),
        prodamus_url_return=_raw("PRODAMUS_URL_RETURN"),
        webhook_host=_raw("WEBHOOK_HOST", "0.0.0.0"),
        webhook_port=_int("WEBHOOK_PORT", _int("PORT", 8080)),
        webhook_path=_normalize_path(_raw("WEBHOOK_PATH", "/prodamus/webhook")),
        public_base_url=_raw("PUBLIC_BASE_URL"),
        db_path=_raw("DB_PATH", "data/club.db"),
        timezone=ZoneInfo(_raw("TZ", "Europe/Moscow")),
        support_contact=_raw("SUPPORT_CONTACT"),
        log_level=(_raw("LOG_LEVEL", "INFO") or "INFO").upper(),
    )


def _normalize_path(path: str | None) -> str:
    path = (path or "/prodamus/webhook").strip()
    if not path.startswith("/"):
        path = "/" + path
    return path
