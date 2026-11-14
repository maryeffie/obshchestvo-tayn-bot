"""Слой работы с БД (SQLite через aiosqlite).

Таблицы:
  users    — участники клуба и их статус доступа
  orders   — платёжные ссылки, которые мы выдавали (order_id -> tg_id)
  payments — журнал всех уведомлений от Prodamus (для аудита и статистики)
  promos   — промокоды на скидку
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Iterable

import aiosqlite

from .utils import now_iso, to_iso, utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    tg_id             INTEGER PRIMARY KEY,
    username          TEXT,
    first_name        TEXT,
    status            TEXT NOT NULL DEFAULT 'new',   -- new | active | expired
    paid_until        TEXT,
    last_payment_at   TEXT,
    reminder_sent_for TEXT,                          -- дата paid_until, за которую уже напомнили
    reminder_stage    INTEGER NOT NULL DEFAULT 0,    -- 0 нет / 1 за N дней / 2 в день окончания / 3 после
    access_source     TEXT,                          -- prodamus | manual | promo
    auto_renew        INTEGER NOT NULL DEFAULT 1,    -- 1 = автопродление включено
    last_currency     TEXT,                          -- валюта прошлой оплаты (eur/usd/byn)
    cancelled_at      TEXT,                          -- когда пользователь отменил подписку
    phone             TEXT,
    email             TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_users_status ON users(status);
CREATE INDEX IF NOT EXISTS idx_users_paid_until ON users(paid_until);

CREATE TABLE IF NOT EXISTS orders (
    order_id     TEXT PRIMARY KEY,
    tg_id        INTEGER NOT NULL,
    amount       INTEGER NOT NULL,
    promo_code   TEXT,
    status       TEXT NOT NULL DEFAULT 'pending',    -- pending | paid | failed | cancelled
    is_recurring INTEGER NOT NULL DEFAULT 0,
    currency     TEXT NOT NULL DEFAULT 'rub',         -- валюта счёта (подписка — только rub)
    created_at   TEXT NOT NULL,
    paid_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_orders_tg ON orders(tg_id);

CREATE TABLE IF NOT EXISTS payments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id    TEXT,
    tg_id       INTEGER,
    amount      INTEGER,
    kind        TEXT,                                -- paid | failed | cancelled | info
    is_renewal  INTEGER NOT NULL DEFAULT 0,
    status_text TEXT,
    raw         TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_payments_tg ON payments(tg_id);

CREATE TABLE IF NOT EXISTS promos (
    code             TEXT PRIMARY KEY COLLATE NOCASE,
    discount_percent INTEGER NOT NULL DEFAULT 0,     -- скидка в процентах от цены
    max_uses         INTEGER NOT NULL DEFAULT 1,     -- 0 = без ограничений
    used_count       INTEGER NOT NULL DEFAULT 0,
    active           INTEGER NOT NULL DEFAULT 1,
    expires_at       TEXT,
    created_at       TEXT NOT NULL
);
"""


class Database:
    """Небольшая обёртка над SQLite. Одна общая коннекция + блокировка на запись."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ служебное

    async def connect(self) -> None:
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA busy_timeout=5000")

    async def init(self) -> None:
        assert self._conn is not None, "Database.connect() не вызван"
        await self._conn.executescript(SCHEMA)
        await self._conn.commit()
        await self._migrate()

    async def _migrate(self) -> None:
        """Досоздаёт колонки, появившиеся в новых версиях (безопасно при апдейте)."""
        async with self.conn.execute("PRAGMA table_info(users)") as cur:
            columns = {row[1] for row in await cur.fetchall()}
        additions = {
            "auto_renew": "ALTER TABLE users ADD COLUMN auto_renew INTEGER NOT NULL DEFAULT 1",
            "cancelled_at": "ALTER TABLE users ADD COLUMN cancelled_at TEXT",
            "reminder_stage": (
                "ALTER TABLE users ADD COLUMN reminder_stage INTEGER NOT NULL DEFAULT 0"
            ),
            "last_currency": "ALTER TABLE users ADD COLUMN last_currency TEXT",
        }
        for column, sql in additions.items():
            if column not in columns:
                await self.conn.execute(sql)

        async with self.conn.execute("PRAGMA table_info(orders)") as cur:
            order_columns = {row[1] for row in await cur.fetchall()}
        if "currency" not in order_columns:
            await self.conn.execute(
                "ALTER TABLE orders ADD COLUMN currency TEXT NOT NULL DEFAULT 'rub'"
            )
        await self.conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database.connect() не вызван")
        return self._conn

    async def _write(self, sql: str, params: Iterable[Any] = ()) -> None:
        async with self._lock:
            await self.conn.execute(sql, tuple(params))
            await self.conn.commit()

    async def _fetchone(self, sql: str, params: Iterable[Any] = ()) -> dict | None:
        async with self.conn.execute(sql, tuple(params)) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def _fetchall(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        async with self.conn.execute(sql, tuple(params)) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    # --------------------------------------------------------------------- users

    async def upsert_user(
        self,
        tg_id: int,
        username: str | None = None,
        first_name: str | None = None,
    ) -> dict:
        now = now_iso()
        await self._write(
            """
            INSERT INTO users (tg_id, username, first_name, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(tg_id) DO UPDATE SET
                username = COALESCE(excluded.username, users.username),
                first_name = COALESCE(excluded.first_name, users.first_name),
                updated_at = excluded.updated_at
            """,
            (tg_id, username, first_name, now, now),
        )
        user = await self.get_user(tg_id)
        assert user is not None
        return user

    async def get_user(self, tg_id: int) -> dict | None:
        return await self._fetchone("SELECT * FROM users WHERE tg_id = ?", (tg_id,))

    async def get_user_by_username(self, username: str) -> dict | None:
        return await self._fetchone(
            "SELECT * FROM users WHERE LOWER(username) = LOWER(?)", (username.lstrip("@"),)
        )

    async def find_user(self, query: str) -> dict | None:
        query = query.strip()
        if query.lstrip("-").isdigit():
            return await self.get_user(int(query))
        return await self.get_user_by_username(query)

    async def get_user_by_contact(self, phone: str | None, email: str | None) -> dict | None:
        if phone:
            digits = _digits(phone)
            if digits:
                user = await self._fetchone(
                    "SELECT * FROM users WHERE phone = ? OR phone = ?", (phone, "+" + digits)
                )
                if user:
                    return user
        if email:
            user = await self._fetchone(
                "SELECT * FROM users WHERE LOWER(email) = LOWER(?)", (email,)
            )
            if user:
                return user
        return None

    async def save_contacts(self, tg_id: int, phone: str | None, email: str | None) -> None:
        await self._write(
            """
            UPDATE users SET
                phone = COALESCE(?, phone),
                email = COALESCE(?, email),
                updated_at = ?
            WHERE tg_id = ?
            """,
            (phone, email, now_iso(), tg_id),
        )

    async def activate(
        self,
        tg_id: int,
        paid_until,
        source: str = "prodamus",
        paid_at=None,
    ) -> dict:
        """Выдаёт/продлевает доступ до paid_until."""
        paid_at = paid_at or utcnow()
        await self._write(
            """
            UPDATE users SET
                status = 'active',
                paid_until = ?,
                last_payment_at = ?,
                reminder_sent_for = NULL,
                reminder_stage = 0,
                access_source = ?,
                auto_renew = 1,
                cancelled_at = NULL,
                updated_at = ?
            WHERE tg_id = ?
            """,
            (to_iso(paid_until), to_iso(paid_at), source, now_iso(), tg_id),
        )
        user = await self.get_user(tg_id)
        assert user is not None
        return user

    async def set_status(self, tg_id: int, status: str) -> None:
        await self._write(
            "UPDATE users SET status = ?, updated_at = ? WHERE tg_id = ?",
            (status, now_iso(), tg_id),
        )

    async def set_last_currency(self, tg_id: int, currency: str) -> None:
        """Запоминаем валюту оплаты: по ней бот предложит продление."""
        await self._write(
            "UPDATE users SET last_currency = ?, updated_at = ? WHERE tg_id = ?",
            (currency.lower(), now_iso(), tg_id),
        )

    async def set_auto_renew(self, tg_id: int, value: bool, cancelled: bool = False) -> None:
        """value=False — автосписание отключено (пользователь или админ)."""
        await self._write(
            """
            UPDATE users SET
                auto_renew = ?,
                cancelled_at = CASE WHEN ? = 0 THEN ? ELSE cancelled_at END,
                reminder_sent_for = CASE WHEN ? = 0 THEN paid_until ELSE reminder_sent_for END,
                reminder_stage = CASE WHEN ? = 0 THEN 99 ELSE reminder_stage END,
                updated_at = ?
            WHERE tg_id = ?
            """,
            (
                int(value),
                int(value),
                now_iso() if cancelled else None,
                int(value),
                int(value),
                now_iso(),
                tg_id,
            ),
        )

    async def mark_reminder_sent(self, tg_id: int, paid_until: str) -> None:
        await self._write(
            "UPDATE users SET reminder_sent_for = ?, updated_at = ? WHERE tg_id = ?",
            (paid_until, now_iso(), tg_id),
        )

    async def active_users(self) -> list[dict]:
        return await self._fetchall("SELECT * FROM users WHERE status = 'active'")

    async def users_for_reminder(self, horizon_iso: str) -> list[dict]:
        """Активные, у кого доступ кончается не позже horizon (какую именно стадию
        напоминания отправлять, решает tasks.py — так их можно сделать несколько)."""
        return await self._fetchall(
            """
            SELECT * FROM users
            WHERE status = 'active'
              AND paid_until IS NOT NULL
              AND COALESCE(auto_renew, 1) = 1
              AND paid_until <= ?
            """,
            (horizon_iso,),
        )

    async def set_reminder_stage(self, tg_id: int, stage: int, paid_until: str) -> None:
        await self._write(
            "UPDATE users SET reminder_stage = ?, reminder_sent_for = ?, updated_at = ? "
            "WHERE tg_id = ?",
            (stage, paid_until, now_iso(), tg_id),
        )

    async def users_to_expire(self, cutoff_iso: str) -> list[dict]:
        """Активные, у кого истёк оплаченный период (отсрочка учитывается в tasks.py)."""
        return await self._fetchall(
            """
            SELECT * FROM users
            WHERE status = 'active'
              AND paid_until IS NOT NULL
              AND paid_until <= ?
            """,
            (cutoff_iso,),
        )

    async def all_users(self, limit: int = 1000) -> list[dict]:
        return await self._fetchall(
            "SELECT * FROM users ORDER BY created_at DESC LIMIT ?", (limit,)
        )

    # -------------------------------------------------------------------- orders

    async def create_order(
        self,
        order_id: str,
        tg_id: int,
        amount: int,
        promo_code: str | None = None,
        is_recurring: bool = False,
        currency: str = "rub",
    ) -> None:
        await self._write(
            """
            INSERT INTO orders
                (order_id, tg_id, amount, promo_code, is_recurring, currency, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                order_id,
                tg_id,
                amount,
                promo_code,
                int(is_recurring),
                currency.lower(),
                now_iso(),
            ),
        )

    async def find_pending_order(self, tg_id: int) -> dict | None:
        """Последний неоплаченный заказ участника — для сверки платежа."""
        return await self._fetchone(
            "SELECT * FROM orders WHERE tg_id = ? AND status = 'pending' "
            "ORDER BY created_at DESC LIMIT 1",
            (tg_id,),
        )

    async def get_order(self, order_id: str) -> dict | None:
        return await self._fetchone("SELECT * FROM orders WHERE order_id = ?", (order_id,))

    async def set_order_status(self, order_id: str, status: str) -> None:
        paid_at = now_iso() if status == "paid" else None
        await self._write(
            "UPDATE orders SET status = ?, paid_at = COALESCE(?, paid_at) WHERE order_id = ?",
            (status, paid_at, order_id),
        )

    # ------------------------------------------------------------------ payments

    async def add_payment(
        self,
        order_id: str | None,
        tg_id: int | None,
        amount: int | None,
        kind: str,
        is_renewal: bool = False,
        status_text: str | None = None,
        raw: dict | None = None,
    ) -> None:
        await self._write(
            """
            INSERT INTO payments (order_id, tg_id, amount, kind, is_renewal, status_text, raw, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                order_id,
                tg_id,
                amount,
                kind,
                int(is_renewal),
                status_text,
                json.dumps(raw, ensure_ascii=False) if raw else None,
                now_iso(),
            ),
        )

    async def last_successful_payment(self, tg_id: int) -> dict | None:
        return await self._fetchone(
            """
            SELECT * FROM payments WHERE tg_id = ? AND kind = 'paid'
            ORDER BY id DESC LIMIT 1
            """,
            (tg_id,),
        )

    # -------------------------------------------------------------------- promos

    async def create_promo(
        self,
        code: str,
        discount_percent: int,
        max_uses: int = 0,
        expires_at: str | None = None,
    ) -> None:
        await self._write(
            """
            INSERT INTO promos (code, discount_percent, max_uses, expires_at, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(code) DO UPDATE SET
                discount_percent = excluded.discount_percent,
                max_uses = excluded.max_uses,
                expires_at = excluded.expires_at,
                active = 1
            """,
            (code.upper(), discount_percent, max_uses, expires_at, now_iso()),
        )

    async def get_promo(self, code: str) -> dict | None:
        return await self._fetchone("SELECT * FROM promos WHERE code = ?", (code.strip().upper(),))

    async def promo_is_valid(self, code: str) -> tuple[bool, str]:
        promo = await self.get_promo(code)
        if not promo:
            return False, "Промокод не найден. Проверьте написание."
        if not promo["active"]:
            return False, "Этот промокод больше не действует."
        if promo["expires_at"] and promo["expires_at"] <= now_iso():
            return False, "Срок действия промокода истёк."
        if promo["max_uses"] and promo["used_count"] >= promo["max_uses"]:
            return False, "Лимит использований промокода исчерпан."
        return True, ""

    async def use_promo(self, code: str) -> None:
        await self._write(
            "UPDATE promos SET used_count = used_count + 1 WHERE code = ?", (code.upper(),)
        )

    async def list_promos(self) -> list[dict]:
        return await self._fetchall("SELECT * FROM promos ORDER BY created_at DESC")

    async def deactivate_promo(self, code: str) -> bool:
        promo = await self.get_promo(code)
        if not promo:
            return False
        await self._write("UPDATE promos SET active = 0 WHERE code = ?", (code.upper(),))
        return True

    # --------------------------------------------------------------------- stats

    async def stats(self) -> dict:
        total = await self._fetchone("SELECT COUNT(*) AS c FROM users")
        active = await self._fetchone(
            "SELECT COUNT(*) AS c FROM users WHERE status = 'active' AND paid_until > ?",
            (now_iso(),),
        )
        revenue = await self._fetchone(
            "SELECT COALESCE(SUM(amount), 0) AS s, COUNT(*) AS c FROM payments WHERE kind = 'paid'"
        )
        month_start = to_iso(utcnow().replace(day=1, hour=0, minute=0, second=0))
        month = await self._fetchone(
            "SELECT COALESCE(SUM(amount), 0) AS s, COUNT(*) AS c FROM payments "
            "WHERE kind = 'paid' AND created_at >= ?",
            (month_start,),
        )
        fresh = await self._fetchone(
            "SELECT COUNT(*) AS c FROM users WHERE created_at >= ?", (month_start,)
        )
        soon = await self._fetchone(
            """
            SELECT COUNT(*) AS c FROM users
            WHERE status = 'active' AND paid_until IS NOT NULL
              AND paid_until > ? AND paid_until <= ?
            """,
            (now_iso(), to_iso(utcnow().replace(microsecond=0) + _days(3))),
        )
        cancelled = await self._fetchone(
            """
            SELECT COUNT(*) AS c FROM users
            WHERE status = 'active' AND paid_until > ? AND COALESCE(auto_renew, 1) = 0
            """,
            (now_iso(),),
        )
        return {
            "users_total": total["c"] if total else 0,
            "users_new_month": fresh["c"] if fresh else 0,
            "active": active["c"] if active else 0,
            "revenue_total": revenue["s"] if revenue else 0,
            "payments_total": revenue["c"] if revenue else 0,
            "revenue_month": month["s"] if month else 0,
            "payments_month": month["c"] if month else 0,
            "expiring_3d": soon["c"] if soon else 0,
            "autorenew_off": cancelled["c"] if cancelled else 0,
        }


def _digits(value: str) -> str:
    return "".join(ch for ch in value if ch.isdigit())


def _days(n: int):
    from datetime import timedelta

    return timedelta(days=n)


__all__ = ["Database", "SCHEMA"]
