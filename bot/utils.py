"""Мелкие утилиты: работа со временем, форматирование сумм, логирование."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

UTC = timezone.utc


def utcnow() -> datetime:
    """Текущее время в UTC с точностью до секунды."""
    return datetime.now(UTC).replace(microsecond=0)


def to_iso(dt: datetime) -> str:
    """Единый формат хранения дат в БД (сравним как строки)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def from_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def now_iso() -> str:
    return to_iso(utcnow())


def fmt_price(amount: int | float, currency: str = "rub") -> str:
    """790 -> '790 ₽'."""
    symbols = {"rub": "₽", "usd": "$", "eur": "€", "kzt": "₸", "byn": "Br"}
    amount = int(round(amount))
    human = f"{amount:,}".replace(",", " ")
    return f"{human} {symbols.get(currency.lower(), currency.upper())}"


def fmt_datetime(dt: datetime | str | None, tz: ZoneInfo | None = None) -> str:
    """Дата для показа пользователю: '04.11.2026 в 18:30'."""
    if isinstance(dt, str):
        dt = from_iso(dt)
    if dt is None:
        return "—"
    if tz is not None:
        dt = dt.astimezone(tz)
    return dt.strftime("%d.%m.%Y в %H:%M")


def fmt_date(dt: datetime | str | None, tz: ZoneInfo | None = None) -> str:
    if isinstance(dt, str):
        dt = from_iso(dt)
    if dt is None:
        return "—"
    if tz is not None:
        dt = dt.astimezone(tz)
    return dt.strftime("%d.%m.%Y")


def days_left(until: datetime | str | None) -> int:
    """Сколько полных дней осталось (может быть отрицательным)."""
    dt = from_iso(until) if isinstance(until, str) else until
    if dt is None:
        return 0
    delta = dt - utcnow()
    return max(0, delta.days) if delta.total_seconds() > 0 else -1


def add_days(dt: datetime, days: int) -> datetime:
    return dt + timedelta(days=days)


def is_active(user: dict | None) -> bool:
    """Есть ли у пользователя действующий доступ прямо сейчас."""
    if not user or user.get("status") != "active":
        return False
    until = from_iso(user.get("paid_until"))
    return until is not None and until > utcnow()


def human_username(user: dict | None) -> str:
    if not user:
        return "—"
    if user.get("username"):
        return "@" + user["username"]
    return str(user.get("tg_id", "—"))


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
