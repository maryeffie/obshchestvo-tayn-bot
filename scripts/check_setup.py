#!/usr/bin/env python3
"""Самопроверка настроек: что заполнено, что не так и что делать дальше.

Запуск (из папки проекта):
    python scripts/check_setup.py

Скрипт ничего не меняет и не запускает бота — только читает .env и
спрашивает Telegram, всё ли на месте.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

OK, WARN, BAD = "✅", "⚠️ ", "❌"
results: list[tuple[str, str, str]] = []  # (иконка, пункт, комментарий)


def add(icon: str, item: str, comment: str = "") -> None:
    results.append((icon, item, comment))


def env(name: str) -> str:
    return (os.getenv(name) or "").strip()


def tg(method: str, token: str, **params) -> tuple[bool, dict]:
    url = f"https://api.telegram.org/bot{token}/{method}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=20) as response:
            return True, json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        try:
            return False, json.loads(exc.read().decode())
        except Exception:  # noqa: BLE001
            return False, {"description": f"HTTP {exc.code}"}
    except Exception as exc:  # noqa: BLE001
        return False, {"description": f"{type(exc).__name__}: {exc}"}


def check_telegram() -> tuple[str, int | None]:
    token = env("BOT_TOKEN")
    if not token:
        add(BAD, "BOT_TOKEN", "не заполнен — возьмите у @BotFather")
        return "", None
    if ":" not in token or len(token) < 30:
        add(BAD, "BOT_TOKEN", "похоже на опечатку: нужен вид 123456789:AA…")
        return token, None

    ok, data = tg("getMe", token)
    if not ok:
        add(BAD, "BOT_TOKEN", f"Telegram не принял токен: {data.get('description')}")
        return token, None
    bot = data["result"]
    add(OK, "BOT_TOKEN", f"бот @{bot['username']} («{bot['first_name']}»)")
    return token, bot["id"]


def check_chat(token: str, bot_id: int | None, var: str, human: str, need_admin: bool = True) -> None:
    raw = env(var)
    if not raw:
        add(BAD, var, f"не заполнен — {human}: нужен ID вида -1001234567890")
        return

    candidates = [raw]
    if raw.lstrip("-").isdigit() and not raw.startswith("-"):
        candidates.insert(0, "-" + raw)  # частая ошибка: скопирован ID без минуса
        candidates.insert(1, "-100" + raw)

    for candidate in candidates:
        ok, data = tg("getChat", token, chat_id=candidate)
        if not ok:
            continue
        chat = data["result"]
        title = chat.get("title") or chat.get("username") or "без названия"
        add(OK, var, f"{human}: «{title}», ID <code>{candidate}</code>")
        if candidate != raw:
            add(WARN, var, f"в .env лучше записать <b>{candidate}</b> (без минуса Telegram не находит чат)")

        if need_admin and bot_id:
            ok2, member = tg("getChatMember", token, chat_id=candidate, user_id=bot_id)
            if not ok2:
                add(BAD, f"{var} → права бота", f"не удалось проверить: {member.get('description')}")
                return
            result = member["result"]
            status = result.get("status")
            if status not in {"administrator", "creator"}:
                add(BAD, f"{var} → права бота", "бот НЕ администратор — добавьте его админом")
                return
            missing = []
            if result.get("can_invite_users") is False:
                missing.append("«Приглашать пользователей»")
            if result.get("can_restrict_members") is False:
                missing.append("«Блокировать участников»")
            if missing:
                add(BAD, f"{var} → права бота", "нет прав: " + ", ".join(missing))
            else:
                add(OK, f"{var} → права бота", "администратор, нужные права есть")
        return

    add(
        BAD,
        var,
        f"{human} не найден. Либо ID неверный, либо бот не добавлен туда администратором "
        f"(пробовали: {', '.join(candidates)})",
    )


def check_prodamus() -> None:
    secret = env("PRODAMUS_SECRET")
    if not secret:
        add(BAD, "PRODAMUS_SECRET", "не заполнен — Настройки → секретный ключ платёжной страницы")
    elif len(secret) < 32:
        add(WARN, "PRODAMUS_SECRET", f"короткий ключ ({len(secret)} символов) — проверьте, что скопирован целиком")
    else:
        add(OK, "PRODAMUS_SECRET", f"заполнен ({secret[:6]}…{secret[-4:]})")

    form = env("PRODAMUS_FORM_URL")
    if not form:
        add(BAD, "PRODAMUS_FORM_URL", "не заполнен — адрес вашей платёжной формы, например https://vasha-forma.payform.ru/")
    elif "demo.payform.ru" in form:
        add(WARN, "PRODAMUS_FORM_URL", "это демо-форма Prodamus. Для боевых платежей укажите адрес вашей формы")
    elif not form.endswith("/"):
        add(WARN, "PRODAMUS_FORM_URL", "лучше со слэшем на конце: " + form + "/")
    else:
        add(OK, "PRODAMUS_FORM_URL", form)

    sub = env("PRODAMUS_SUBSCRIPTION_ID")
    if sub.isdigit():
        add(OK, "PRODAMUS_SUBSCRIPTION_ID", f"автосписания включены (товар {sub})")
    elif sub:
        add(WARN, "PRODAMUS_SUBSCRIPTION_ID", "должно быть число — ID подписного товара")
    else:
        add(
            WARN,
            "PRODAMUS_SUBSCRIPTION_ID",
            "автосписаний не будет: бот работает как разовая оплата 790 ₽ "
            "(включается после подключения подписок в Prodamus)",
        )


def check_other() -> None:
    admin = env("ADMIN_IDS")
    if admin and admin.split(",")[0].strip().isdigit():
        add(OK, "ADMIN_IDS", admin)
    else:
        add(BAD, "ADMIN_IDS", "нужен ваш числовой Telegram ID")

    base = env("PUBLIC_BASE_URL")
    if not base:
        add(BAD, "PUBLIC_BASE_URL", "не заполнен — понадобится после установки на сервер")
    elif not base.startswith("https://"):
        add(WARN, "PUBLIC_BASE_URL", "Prodamus требует https://")
    else:
        add(OK, "PUBLIC_BASE_URL", base + env("WEBHOOK_PATH"))

    price = env("PRICE") or "790"
    add(OK, "PRICE", f"{price} ₽ / {env('SUBSCRIPTION_DAYS') or '30'} дней")


def main() -> int:
    print()
    print("Проверка настроек бота «Общество тайн»")
    print("=" * 60)

    token, bot_id = check_telegram()
    if token:
        check_chat(token, bot_id, "CHANNEL_ID", "канал")
        check_chat(token, bot_id, "CHAT_ID", "чат обсуждений")
    check_prodamus()
    check_other()

    for icon, item, comment in results:
        print(f"{icon} {item}")
        if comment:
            print(f"     {comment}")

    bad = sum(1 for icon, _, _ in results if icon == BAD)
    warn = sum(1 for icon, _, _ in results if icon == WARN)
    print("=" * 60)
    if bad:
        print(f"Итого: {bad} обязательных пунктов нужно поправить, предупреждений: {warn}.")
        print("Бот не запустится, пока не заполнены обязательные пункты (❌).")
        return 1
    if warn:
        print(f"Обязательное всё заполнено. Предупреждений: {warn} — бот запустится.")
        return 0
    print("Всё заполнено. Можно запускать: python run.py (или docker compose up -d).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
