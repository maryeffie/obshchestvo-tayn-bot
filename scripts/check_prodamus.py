#!/usr/bin/env python3
"""Проверка связки с Prodamus: принимает ли форма наши подписи и ссылки.

    python scripts/check_prodamus.py

Скрипт ничего не оплачивает: он формирует тестовую ссылку на 1 ₽ с подписью,
открывает её и смотрит реакцию формы:
    HTTP 200 — подпись принята, форма отрисуется покупателю;
    HTTP 3xx — Prodamus отбросил параметры (обычно из-за подписи).

Если основной вариант отклонён, скрипт перебирает альтернативные форматы
подписи и подсказывает, какой принимает ваша форма.
"""

from __future__ import annotations

import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

from bot.prodamus import _flatten, _stringify, sign_payload  # noqa: E402

load_dotenv(ROOT / ".env")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


def http_status(url: str, timeout: int = 20) -> int:
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(url, timeout=timeout) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except Exception as exc:  # noqa: BLE001
        print(f"    сетевая ошибка: {type(exc).__name__}: {exc}")
        return 0


def build_url(form: str, payload: dict) -> str:
    return form + "?" + urlencode(list(_flatten(_stringify(payload))))


def main() -> int:
    form = (os.getenv("PRODAMUS_FORM_URL") or "").strip()
    secret = (os.getenv("PRODAMUS_SECRET") or "").strip()
    price = (os.getenv("PRICE") or "790").strip()
    product = (os.getenv("PRODUCT_NAME") or "Подписка").strip()

    print()
    print("Проверка связки с Prodamus")
    print("=" * 60)

    if not form or not secret:
        print("❌ Нужны PRODAMUS_FORM_URL и PRODAMUS_SECRET в .env")
        return 1
    print(f"Форма:  {form}")
    print(f"Ключ:   {secret[:6]}…{secret[-4:]}  ({len(secret)} символов)")
    print(f"Проверю подпись на тестовом заказе на {price} ₽ (оплата НЕ пройдёт).")
    print()

    base = {
        "do": "pay",
        "order_id": "check-signature-nopay",
        "customer_extra": "tg:0",
        "products": [{"name": product, "price": price, "quantity": "1", "type": "service"}],
    }

    variants = {
        "UTF-8 JSON + экранированные «/» (наш вариант)": sign_payload(base, secret),
        "UTF-8 JSON без экранирования «/»": sign_payload(base, secret, escape_slashes=False),
        "JSON с \\uXXXX вместо кириллицы": sign_payload(base, secret, ensure_ascii=True),
    }

    results: dict[str, int] = {}
    for name, signature in variants.items():
        status = http_status(build_url(form, {**base, "signature": signature}))
        results[name] = status
        icon = "✅" if status == 200 else ("⚠️ " if status == 0 else "❌")
        print(f"{icon} {name}: HTTP {status}")

    control = http_status(build_url(form, base))
    print(f"   контроль (ссылка вообще без подписи): HTTP {control}")
    print()

    main_status = results["UTF-8 JSON + экранированные «/» (наш вариант)"]
    if main_status == 200:
        print("✅ Подпись принимается. Ссылки на оплату из бота будут открываться.")
        print("   Адрес для уведомлений Prodamus:")
        print(f"   {(os.getenv('PUBLIC_BASE_URL') or 'https://ВАШ-ДОМЕН').rstrip('/')}"
              f"{os.getenv('WEBHOOK_PATH') or '/prodamus/webhook'}")
        return 0

    print("❌ Основной вариант подписи отклонён.")
    working = [name for name, status in results.items() if status == 200]
    if working:
        print("   Форма принимает другой вариант: " + ", ".join(working))
        print("   Сообщите об этом разработчику — нужно поменять формат подписи в bot/prodamus.py.")
    else:
        print("   Ни один вариант не принят. Проверьте:")
        print("   • секретный ключ относится именно к этой платёжной странице;")
        print("   • на форме включён боевой режим (демо-ключ не подойдёт);")
        print("   • адрес формы начинается с https и заканчивается слэшем.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
